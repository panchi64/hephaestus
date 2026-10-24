"""Database engine and the sync-in-threadpool access pattern.

The connection is built in one place (:func:`make_engine`). SQLite's driver is
synchronous, so every unit of DB work runs in a thread to keep the event loop free
(SQLite releases the GIL during I/O).
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable, Generator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import NoReturn, Protocol
from weakref import WeakKeyDictionary

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from alembic.util.exc import CommandError
from sqlalchemy import Connection, Engine, event, inspect
from sqlalchemy.dialects.postgresql import Insert as PostgresInsert
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import Insert as SQLiteInsert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine.interfaces import DBAPIConnection
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.pool import ConnectionPoolEntry, StaticPool
from sqlmodel import Session, SQLModel, create_engine

from core.exceptions import NotFoundError, SchemaMigrationError

logger = logging.getLogger(__name__)

# In-memory engines share one connection (StaticPool), unsafe under the concurrent
# threadpool access `in_session` produces, so a per-engine lock serializes their sessions.
# File-backed engines give each thread its own connection and carry no lock.
_CONN_LOCKS: WeakKeyDictionary[Engine, threading.Lock] = WeakKeyDictionary()

# alembic.ini sits at the backend root; its script_location is `%(here)s/migrations`, so
# resolution is cwd-independent.
ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"

# How long a connection waits for SQLite's single write lock before "database is locked".
# The write-behind drainers (conversations, notifications, corpus, scheduler) routinely
# collide on it; 5s outlasts their short transactions.
BUSY_TIMEOUT_MS = 5000

# Retries for a write that still lost the lock after that budget, with linearly growing delay.
#
# The busy handler parks a threadpool thread for the full wait and then hard-errors on what
# is transient. Concurrent agent turns (ordinary now that agents launch sub-agents) plus the
# drainers can hold the writer lock past it. Retried here rather than per store because this
# module owns how work reaches SQLite. Only lock failures retry: any other `OperationalError`
# (missing column, broken file) is a real fault.
#
# The work callable is re-run from the top, so it must be idempotent; every caller's is
# (`session.get` plus mutate, or an `add` of a row built outside).
LOCK_RETRIES = 3
_LOCK_RETRY_DELAY_S = 0.25


_BLOCKING_ATTEMPTS = 3
_BLOCKING_BUSY_MS = 250
_BLOCKING_RETRY_DELAY_S = 0.05


def _is_locked(exc: OperationalError) -> bool:
    """Whether this is SQLite's writer-lock contention rather than a real fault.

    Matched on the message: `sqlite3` raises the same `OperationalError` for a locked
    database and a syntax error.
    """
    message = str(exc.orig if exc.orig is not None else exc).lower()
    return "database is locked" in message or "database table is locked" in message


def make_engine(url: str) -> Engine:
    """Build the SQLite engine. In-memory URLs share one connection (for tests)."""
    kwargs: dict[str, object] = {"connect_args": {"check_same_thread": False}}
    if ":memory:" in url:
        kwargs["poolclass"] = StaticPool
    engine = create_engine(url, **kwargs)

    if ":memory:" in url:
        _CONN_LOCKS[engine] = threading.Lock()

    @event.listens_for(engine, "connect")
    def _configure_connection(
        dbapi_connection: DBAPIConnection, _record: ConnectionPoolEntry
    ) -> None:
        # pysqlite runs DDL in autocommit and opens a transaction only before the first DML,
        # so a migration failing partway strands its committed leading statement (e.g. a
        # batch rebuild's `_alembic_tmp_*` table) and wedges the next startup. Isolation
        # level None hands transaction control to SQLAlchemy, whose explicit BEGIN (the
        # "begin" handler) makes DDL transactional so a failed migration rolls back whole.
        dbapi_connection.isolation_level = None

        # Foreign keys are off per connection by default; without this the declared FKs
        # enforce nothing and a stray conversation_id silently orphans rows.
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")

        # Without a busy handler a connection finding the write lock held errors
        # immediately, turning a microseconds-long overlap between threadpool sessions into
        # a hard `database is locked`. Per-connection like foreign_keys.
        cursor.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        cursor.close()

    @event.listens_for(engine, "begin")
    def _emit_begin(connection: Connection) -> None:
        # With isolation_level None above, SQLAlchemy must emit BEGIN or statements autocommit.
        _ = connection.exec_driver_sql("BEGIN")

    return engine


def init_db(engine: Engine) -> None:
    """Bring the schema to head via Alembic on startup.

    Migrations run against the live engine (handed to ``migrations/env.py`` on the config),
    not one built from a URL, since an in-memory test DB's schema lives only on its one connection.

    Guards the two ways a dev DB drifts: a stamp at a deleted or regenerated revision, and a
    table predating a reworked migration while the stamp claims head (``no such column``).
    Either fails the boot with a one-line diagnostic (DB path, stamped vs head revision)
    instead of a deep traceback.
    """
    config = Config(str(ALEMBIC_INI))
    config.attributes["connection"] = engine
    try:
        command.upgrade(config, "head")
    except (CommandError, OperationalError) as exc:
        _raise_schema_error(config, engine, cause=exc)

    drift = _schema_drift(engine)
    if drift:
        _raise_schema_error(config, engine, drift=drift)


def _stamped_revisions(engine: Engine) -> tuple[str, ...]:
    """The revision(s) in ``alembic_version`` (empty for a fresh DB), read off the connection
    so an orphaned stamp comes back verbatim."""
    with engine.connect() as connection:
        return MigrationContext.configure(connection).get_current_heads()


def _schema_drift(engine: Engine) -> list[str]:
    """Tables and columns the models declare that the live DB lacks, meaning the recorded
    revision and physical schema disagree. Only missing ones, never type or extra-column
    differences, which can read benignly across SQLite round-trips."""
    inspector = inspect(engine)
    existing = set(inspector.get_table_names())
    problems: list[str] = []
    for table in SQLModel.metadata.sorted_tables:
        if table.name not in existing:
            problems.append(f"table {table.name!r} is missing entirely")
            continue
        actual = {column["name"] for column in inspector.get_columns(table.name)}
        missing = [c.name for c in table.columns if c.name not in actual]
        if missing:
            problems.append(f"table {table.name!r} is missing column(s): {', '.join(missing)}")
    return problems


def _raise_schema_error(
    config: Config,
    engine: Engine,
    *,
    cause: Exception | None = None,
    drift: list[str] | None = None,
) -> NoReturn:
    """Log a diagnostic (DB path, stamped vs head revision, cause or drift), then raise,
    in place of the raw traceback."""
    head = ScriptDirectory.from_config(config).get_current_head()
    try:
        stamped = ", ".join(_stamped_revisions(engine)) or "(none — fresh database)"
    except SQLAlchemyError:  # pragma: no cover - the bookkeeping table itself is unreadable
        stamped = "(unreadable)"

    lines = [
        "Database schema migration failed.",
        f"  database:      {engine.url.render_as_string(hide_password=True)}",
        f"  stamped in DB: {stamped}",
        f"  head in code:  {head}",
    ]
    if cause is not None:
        lines.append(f"  cause:         {type(cause).__name__}: {cause}")
    if drift:
        lines.append("  schema drift (stamped at head, but does not match the models):")
        lines.extend(f"    - {problem}" for problem in drift)
    lines.append(
        "  The recorded revision and/or the physical schema disagree with the "
        + "migration scripts — typically a dev DB left stamped at a deleted or "
        + "regenerated migration. Reconcile with Alembic (stamp to the matching "
        + "revision, or drop and re-create the drifted table) before restarting."
    )
    message = "\n".join(lines)
    logger.error(message)
    raise SchemaMigrationError(message) from None


async def in_session[T](engine: Engine, work: Callable[[Session], T]) -> T:
    """Run a unit of DB work in a threadpool and commit it, under the engine's lock for an
    in-memory engine. A write losing the writer lock past ``busy_timeout`` is retried
    (:data:`LOCK_RETRIES`).
    """
    lock = _CONN_LOCKS.get(engine)

    def _run() -> T:
        with Session(engine, expire_on_commit=False) as session:
            result = work(session)
            session.commit()
            return result

    def _run_guarded() -> T:
        if lock is None:
            return _run()
        with lock:
            return _run()

    for attempt in range(LOCK_RETRIES + 1):
        try:
            return await asyncio.to_thread(_run_guarded)
        except OperationalError as exc:
            if attempt == LOCK_RETRIES or not _is_locked(exc):
                raise
            logger.warning(
                "db: write lock still held after %sms, retrying (%s/%s)",
                BUSY_TIMEOUT_MS,
                attempt + 1,
                LOCK_RETRIES,
            )
            await asyncio.sleep(_LOCK_RETRY_DELAY_S * (attempt + 1))
    raise AssertionError("unreachable")  # pragma: no cover


class _Owned(Protocol):
    owner_id: str


async def get_owned[M: _Owned](
    engine: Engine, model_cls: type[M], row_id: str, owner_id: str, *, what: str
) -> M:
    """The row ``row_id`` names, provided ``owner_id`` owns it; else ``NotFoundError`` naming
    ``what`` (``"endpoint"``, ``"memory"``, …).

    "Not yours" and "doesn't exist" answer identically: distinguishing them would confirm a
    guessed id is real.
    """

    def work(session: Session) -> M | None:
        row = session.get(model_cls, row_id)
        return row if row is not None and row.owner_id == owner_id else None

    row = await in_session(engine, work)
    if row is None:
        raise NotFoundError(f"{what} {row_id!r} not found")
    return row


def upsert(engine: Engine, model_cls: type[SQLModel]) -> SQLiteInsert | PostgresInsert:
    """An INSERT construct carrying ``on_conflict_do_update``, for this engine's dialect.

    Atomic get-or-create needs the conflict handled in the database (select-then-insert
    races into an IntegrityError). The clause is dialect-specific and ``db_url`` is
    configurable, so importing the SQLite insert directly would bind a store to SQLite.
    SQLite and PostgreSQL share the syntax; any other dialect raises here, named.
    """
    name = engine.dialect.name
    if name == "sqlite":
        return sqlite_insert(model_cls)
    if name == "postgresql":
        return postgresql_insert(model_cls)
    raise NotImplementedError(
        f"upsert is not implemented for the {name!r} dialect "
        + "(ON CONFLICT DO UPDATE is SQLite/PostgreSQL syntax)"
    )


@contextmanager
def read_session(engine: Engine) -> Generator[Session]:
    """A read-only session for a caller off the event loop, under the per-engine lock
    :func:`in_session` uses; nothing is committed on exit.

    A bare ``Session(engine)`` beside a running service is unsafe on an in-memory engine:
    a second BEGIN on the shared connection fails with "cannot start a transaction within a
    transaction". Chiefly for tests asserting while the service under test still drives the DB.
    """
    lock = _CONN_LOCKS.get(engine)
    with ExitStack() as stack:
        if lock is not None:
            _ = stack.enter_context(lock)
        yield stack.enter_context(Session(engine, expire_on_commit=False))


def read_blocking[T](engine: Engine, work: Callable[[Session], T]) -> T:
    """Run a read on the calling thread for a synchronous path that can't await. It blocks
    the loop, so it waits on a locked database about a second at most
    (:data:`_BLOCKING_ATTEMPTS` tries of :data:`_BLOCKING_BUSY_MS`) before raising."""

    def capped(session: Session) -> T:
        connection = session.connection()
        sqlite = connection.dialect.name == "sqlite"
        previous = connection.exec_driver_sql("PRAGMA busy_timeout").scalar() if sqlite else None
        if sqlite:
            _ = connection.exec_driver_sql(f"PRAGMA busy_timeout={_BLOCKING_BUSY_MS}")
        try:
            return work(session)
        finally:
            if sqlite:
                _ = connection.exec_driver_sql(f"PRAGMA busy_timeout={previous}")

    for attempt in range(_BLOCKING_ATTEMPTS):
        try:
            with read_session(engine) as session:
                return capped(session)
        except OperationalError as exc:
            if attempt == _BLOCKING_ATTEMPTS - 1 or not _is_locked(exc):
                raise
            time.sleep(_BLOCKING_RETRY_DELAY_S * (attempt + 1))
    raise AssertionError("unreachable")  # pragma: no cover
