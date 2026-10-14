"""Alembic environment.

Two ways in:

- **Runtime (startup auto-upgrade).** ``core.db.init_db`` puts the live engine on
  ``config.attributes['connection']`` and calls ``command.upgrade(..., 'head')``.
  We migrate on *that* engine — crucial for the in-memory test DBs, whose schema
  lives only on a single shared connection.
- **CLI (autogenerate / manual).** No connection is attached, so we build one
  from ``sqlalchemy.url`` (``alembic.ini``, overridable via ``HEPHAESTUS_DB_URL``).

``target_metadata`` is the SQLModel registry, populated by importing every model
module, so ``--autogenerate`` sees the full schema. SQLite gets batch mode, since
several ``ALTER`` shapes require the table-rebuild dance there.
"""

from __future__ import annotations

import os

from alembic import context
from sqlalchemy import Engine, create_engine
from sqlmodel import SQLModel

from core.exceptions import SchemaMigrationError

# Import every model module so its tables register on SQLModel.metadata — walked
# from the package, so a new entity registers by existing, never by being listed.
from models._discovery import import_all_models

import_all_models()

config = context.config
target_metadata = SQLModel.metadata


def _set_sqlite_foreign_keys(connection, *, enabled: bool) -> None:
    """Toggle SQLite foreign-key enforcement on the raw DBAPI connection.

    ``make_engine`` turns enforcement on for every connection, but batch migrations
    rebuild a table by creating a copy, dropping the original, and renaming the copy
    into place — and dropping a table that other rows still reference fails while
    enforcement is on. SQLite honours this pragma only *outside* a transaction, so it
    runs on the DBAPI connection directly, bypassing SQLAlchemy's autobegin (a pragma
    issued through the SQLAlchemy connection would land inside a transaction, where
    SQLite silently ignores it).
    """
    dbapi_connection = connection.connection
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute(f"PRAGMA foreign_keys={'ON' if enabled else 'OFF'}")
    finally:
        cursor.close()


def _assert_foreign_key_integrity(connection) -> None:
    """Re-validate referential integrity after the batch rebuilds.

    Enforcement is off while migrating, so SQLite accepts the table-rebuild's
    intermediate states without complaint. ``PRAGMA foreign_key_check`` re-checks every
    row against its constraints regardless of the enforcement flag; any rows it returns
    mean a migration left a dangling reference, which must fail the boot rather than
    re-enable enforcement over inconsistent data. This is the verification half of
    SQLite's prescribed "disable FKs → rebuild → check → re-enable" rebuild procedure.
    """
    dbapi_connection = connection.connection
    cursor = dbapi_connection.cursor()
    try:
        violations = cursor.execute("PRAGMA foreign_key_check").fetchall()
    finally:
        cursor.close()
    if violations:
        raise SchemaMigrationError(f"foreign-key integrity broken by migration: {violations}")


def _run(connection) -> None:
    # SQLite can't ALTER in place, so batch mode rebuilds the table (create copy → drop
    # original → rename). Dropping a table other rows still reference fails while FK
    # enforcement is on — and SQLite only lets the pragma change outside a transaction —
    # so drop enforcement up front, verify integrity once the rebuilds land, then restore
    # it. The in-memory test connection is reused after migration, so it must end on.
    is_sqlite = connection.dialect.name == "sqlite"
    if is_sqlite:
        _set_sqlite_foreign_keys(connection, enabled=False)
    try:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,  # SQLite-safe ALTERs (table-rebuild under the hood)
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()
        if is_sqlite:
            _assert_foreign_key_integrity(connection)
    finally:
        if is_sqlite:
            _set_sqlite_foreign_keys(connection, enabled=True)


def run_migrations_online() -> None:
    attached = config.attributes.get("connection")
    if attached is not None:
        # A live engine or connection handed in by the app at startup.
        if isinstance(attached, Engine):
            with attached.connect() as connection:
                _run(connection)
        else:
            _run(attached)
        return

    url = os.environ.get("HEPHAESTUS_DB_URL") or config.get_main_option("sqlalchemy.url")
    if url is None:
        raise SchemaMigrationError("No database URL: set HEPHAESTUS_DB_URL or sqlalchemy.url.")
    engine = create_engine(url)
    with engine.connect() as connection:
        _run(connection)


def run_migrations_offline() -> None:
    """Emit SQL without a DBAPI connection (``alembic upgrade --sql``)."""
    url = os.environ.get("HEPHAESTUS_DB_URL") or config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        render_as_batch=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
