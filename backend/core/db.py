"""Database engine and the sync-in-threadpool access pattern.

Persistence runs on **plain SQLite** for now. The connection is built in one
place (:func:`make_engine`) so at-rest encryption can swap in here later — once
auth exists to derive a key from — without touching any caller. SQLite's driver
is synchronous, so every unit of DB work runs in a thread to keep the event loop
free, and genuinely parallelizes (SQLite releases the GIL during I/O).
"""

from __future__ import annotations

import asyncio
import logging
import threading
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
from sqlalchemy.exc import OperationalError
from sqlalchemy.pool import ConnectionPoolEntry, StaticPool
from sqlmodel import Session, SQLModel, create_engine

from core.exceptions import NotFoundError, SchemaMigrationError

logger = logging.getLogger(__name__)

# In-memory engines share a single connection (StaticPool), which is *not* safe for
# the concurrent, multi-threaded access `in_session` produces (each call runs on its
# own threadpool thread). A per-engine lock serializes their sessions so two threads
# never drive the one connection at once. File-backed engines hand each thread its
# own connection and carry no lock, so they run fully unserialized.
_CONN_LOCKS: WeakKeyDictionary[Engine, threading.Lock] = WeakKeyDictionary()

# alembic.ini lives at the backend root (core/db.py is backend/core/db.py); its
# script_location is `%(here)s/migrations`, so resolution is cwd-independent.
ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"

# How long a connection waits for SQLite's single write lock before erroring with
# "database is locked". File-backed engines hand every threadpool thread its own
# connection, so the write-behind drainers (conversations, notifications, corpus,
# the scheduler) routinely collide on that one lock — 5s comfortably outlasts any
# of their short bookkeeping transactions.
BUSY_TIMEOUT_MS = 5000

# How many times a write that *still* lost the lock after that budget is tried again, and
# how long it waits between tries (growing linearly).
#
# The busy handler is a per-connection wait and it is not the whole answer: it holds the
# calling **thread**, so a long contended stretch is a threadpool thread parked for five
# seconds, and when it does expire the caller gets a hard error for what is a transient
# condition. Two agent turns running at once — which is the ordinary case now that the
# agent launches sub-agents, and was the rare one before — plus the write-behind drainers
# can hold the single writer lock past that budget.
#
# Here rather than in each store, for the reason the busy handler is here: this module owns
# how a unit of work reaches SQLite, and a retry written at one call site is a retry the
# next writer does not get. Only a *lock* failure is retried — every other
# `OperationalError` (a missing column, a broken file) is a real fault that retrying turns
# into a slow real fault.
#
# The work callable is re-run from the top, so it must be idempotent. Every caller's
# already is: they are `session.get` + mutate, or an `add` of a row built outside.
LOCK_RETRIES = 3
_LOCK_RETRY_DELAY_S = 0.25


def _is_locked(exc: OperationalError) -> bool:
    """Whether this is SQLite's writer-lock contention rather than a real fault.

    Matched on the message because that is what SQLite gives us — `sqlite3` raises the
    same `OperationalError` class for a locked database and for a syntax error, and the
    dialect does not translate either into anything more specific.
    """
    message = str(exc.orig if exc.orig is not None else exc).lower()
    return "database is locked" in message or "database table is locked" in message


def make_engine(url: str) -> Engine:
    """Build the SQLite engine. In-memory URLs share one connection (for tests)."""
    kwargs: dict[str, object] = {"connect_args": {"check_same_thread": False}}
    if ":memory:" in url:
        kwargs["poolclass"] = StaticPool
    engine = create_engine(url, **kwargs)

    # A single-connection (in-memory) engine must serialize its threadpool sessions;
    # see `_CONN_LOCKS`. File-backed engines pool a connection per thread and skip it.
    if ":memory:" in url:
        _CONN_LOCKS[engine] = threading.Lock()

    @event.listens_for(engine, "connect")
    def _configure_connection(
        dbapi_connection: DBAPIConnection, _record: ConnectionPoolEntry
    ) -> None:
        # pysqlite ships a legacy transaction model: it runs DDL in autocommit and opens
        # a transaction only before the first DML. That makes schema migrations
        # non-atomic — a migration that fails partway leaves its already-committed
        # leading statement (e.g. a batch rebuild's `_alembic_tmp_*` staging table)
        # stranded, with nothing to roll it back, wedging the next startup. Setting the
        # DBAPI isolation level to None hands transaction control to SQLAlchemy, which
        # then issues an explicit BEGIN (see the "begin" handler) so DDL is fully
        # transactional and a failed migration rolls back whole, residue and all.
        dbapi_connection.isolation_level = None

        # SQLite leaves foreign keys *off* per connection unless asked — without this
        # the declared FKs (e.g. Message → Conversation) enforce nothing, so a stray
        # conversation_id would silently orphan rows. Turn it on for every connection.
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")

        # SQLite admits one writer at a time, and without a busy handler a connection
        # that finds the write lock held errors *immediately* instead of waiting — so
        # two concurrent threadpool sessions (say, a write-behind drainer flushing
        # while the scheduler finalizes a task run) turn a microseconds-long overlap
        # into a hard `database is locked`. A busy_timeout makes the loser wait the
        # lock out and only error once the budget is truly exhausted. Per-connection,
        # like foreign_keys, so it rides along on every pooled connect.
        cursor.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        cursor.close()

    @event.listens_for(engine, "begin")
    def _emit_begin(connection: Connection) -> None:
        # With pysqlite handed over to autocommit above, SQLAlchemy must emit the BEGIN
        # itself; without this no transaction is ever opened and every statement commits.
        _ = connection.exec_driver_sql("BEGIN")

    return engine


def init_db(engine: Engine) -> None:
    """Bring the schema to head via Alembic — applied automatically on startup,
    no manual step (XC-DATA-2).

    Migrations run against the **live engine** (handed to ``migrations/env.py`` on
    the Alembic config), not a fresh one built from a URL — essential for the
    in-memory test DBs, whose schema lives only on a single shared connection.

    Guarded against the two ways a dev DB drifts from its migrations: a stamp at a
    deleted/regenerated revision (the upgrade can't trace from it) and a table whose
    physical shape predates a reworked migration while the stamp still claims head
    (the ``no such column`` class). Either fails the boot with a one-line diagnostic
    — DB path and *stamped vs head* revision — instead of a deep migration traceback.
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
    """The revision(s) recorded in the DB's ``alembic_version`` table (empty for a
    fresh DB). Read directly off the connection, so an *orphaned* stamp comes back
    verbatim rather than failing to resolve against the scripts."""
    with engine.connect() as connection:
        return MigrationContext.configure(connection).get_current_heads()


def _schema_drift(engine: Engine) -> list[str]:
    """Tables/columns the models declare that the live DB lacks. A clean DB matches
    its autogenerated migrations exactly, so any drift means the recorded revision
    and the physical schema disagree — caught here at boot instead of later in a
    service. Conservative on purpose: only missing tables and columns, never type or
    extra-column differences (which can read benignly across SQLite round-trips)."""
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
    """Log an actionable diagnostic — DB path, stamped vs head revision, the failing
    cause or the drift — then raise. Surfaces the one fact the operator needs (the DB
    and the scripts disagree, and on which revision) in place of the raw traceback."""
    head = ScriptDirectory.from_config(config).get_current_head()
    try:
        stamped = ", ".join(_stamped_revisions(engine)) or "(none — fresh database)"
    except Exception:  # pragma: no cover - the bookkeeping table itself is unreadable
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
    """Run a unit of DB work in a threadpool and commit it. For a single-connection
    in-memory engine the session is taken under that engine's lock, so overlapping
    threadpool calls never drive the one shared connection at the same time.

    A write that loses SQLite's single writer lock for longer than ``busy_timeout`` is
    retried rather than raised — see :data:`LOCK_RETRIES`.
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
    """The row ``row_id`` names, provided ``owner_id`` owns it. Raises ``NotFoundError``
    otherwise, naming ``what`` (``"endpoint"``, ``"memory"``, …) in the message.

    Every owner-scoped store had its own copy of this three-line read. It is the boundary
    check on nearly every single-row lookup in the app, and it is worth having exactly one
    of: notably, "you don't own it" and "it doesn't exist" must answer identically —
    distinguishing them tells a caller that an id it guessed is real, which is the leak
    the ownership check exists to prevent.
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

    Atomic get-or-create needs the conflict resolution pushed into the database — a
    select-then-insert races two concurrent callers into a duplicate-key IntegrityError.
    The clause that does it is dialect-specific, and ``db_url`` is configurable, so
    importing ``dialects.sqlite.insert`` directly silently hard-binds a store to SQLite
    and fails at runtime on any other backend.

    SQLite and PostgreSQL spell it the same way. Anything else raises here, naming the
    dialect, rather than failing further in with a syntax error from the driver.
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
    """A session for a caller that is **not** on the event loop, taken under the same
    per-engine lock :func:`in_session` uses.

    Opening a bare ``Session(engine)`` beside a running service is safe on a file-backed
    engine (a connection per thread) and unsafe on an in-memory one (a single shared
    connection): the second BEGIN on a connection that already has a transaction open
    fails with "cannot start a transaction within a transaction". This is the same rule
    as `in_session`, for callers that can't await it — chiefly a test asserting against
    the database while the service under test is still driving it.

    Nothing is committed on exit; this is for reading.
    """
    lock = _CONN_LOCKS.get(engine)
    with ExitStack() as stack:
        if lock is not None:
            _ = stack.enter_context(lock)
        yield stack.enter_context(Session(engine, expire_on_commit=False))
