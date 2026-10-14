"""One migrated schema, built once per session and copied into every test database.

`core.db.init_db` replays the whole Alembic chain, and most of those revisions are
SQLite batch rebuilds — the dialect can't alter a column in place, so it copies the
table to rewrite it. That is ~115 ms, and the suite builds a database per test, so the
default path spends over a minute per run replaying the same revisions to arrive at the
same schema.

The schema at head is deterministic, so it is built here **through the real chain** once
and then copied byte-for-byte into each fresh database with SQLite's own backup API. The
copy is not an approximation of the migrated schema: it *is* the migrated schema, pages
and `alembic_version` stamp included, which is why nothing has to be kept in step by hand
when a revision lands.

`real_init_db` stays exported for `test_migrations.py`, which is about the chain itself
and so has to run it.
"""

from __future__ import annotations

import sqlite3
import threading

from sqlalchemy import Engine
from sqlalchemy.pool import PoolProxiedConnection

import core.db

#: The unpatched startup path. Held before `conftest` swaps the fast one in, so the
#: template below is built by the same code production boots with.
real_init_db = core.db.init_db

_lock = threading.Lock()
_template: sqlite3.Connection | None = None
#: The pooled wrapper the in-memory template lives on. An in-memory database exists only
#: as long as a connection to it does, so this reference is what keeps the template
#: alive for the session — dropping it would collect the schema out from under us.
_template_pin: PoolProxiedConnection | None = None


def _driver_connection(pooled: PoolProxiedConnection) -> sqlite3.Connection:
    """The `sqlite3.Connection` under a pooled wrapper — narrowed, since the pool types
    it as the generic DBAPI protocol and only SQLite's carries `backup`."""
    connection = pooled.driver_connection
    assert isinstance(connection, sqlite3.Connection)
    return connection


def _template_connection() -> sqlite3.Connection:
    global _template, _template_pin
    if _template is None:
        engine = core.db.make_engine("sqlite:///:memory:")
        real_init_db(engine)
        _template_pin = engine.raw_connection()
        _template = _driver_connection(_template_pin)
    return _template


def init_db(engine: Engine) -> None:
    """Bring `engine` to head from the cached schema, or through the real chain.

    Only a database with no tables at all takes the copy. Anything already carrying a
    schema may be mid-chain, stamped at an orphaned revision, or drifted from its
    migrations, and judging which is exactly what the real path exists to do — so a
    second `init_db` on the same engine (the idempotency and guard cases) still runs it.
    """
    raw = engine.raw_connection()
    try:
        destination = _driver_connection(raw)
        # Read through the driver rather than SQLAlchemy's inspector: a bare SELECT on a
        # connection handed `isolation_level = None` opens no transaction. The cursor is
        # closed explicitly before the backup — an unfinished read statement holds the
        # destination open, and `backup` refuses a destination that is still in use.
        cursor = destination.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' LIMIT 1")
        try:
            tables = bool(cursor.fetchall())
        finally:
            cursor.close()
        if not tables:
            # Serialized because every caller shares the one template connection, and
            # `in_session` runs on threadpool threads.
            with _lock:
                _template_connection().backup(destination)
    finally:
        raw.close()

    if tables:
        real_init_db(engine)
