"""Schema is brought to head by Alembic on startup (no manual step)."""

from __future__ import annotations

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text
from sqlmodel import Session

from core.db import _ALEMBIC_INI, make_engine
from core.exceptions import SchemaMigrationError

# The rest of the suite gets a cached copy of the schema instead of replaying the chain
# (see `_schema`). These tests are *about* the chain, so they take the real one.
from ._schema import real_init_db as init_db


def _head_revision() -> str:
    script = ScriptDirectory.from_config(Config(str(_ALEMBIC_INI)))
    head = script.get_current_head()
    assert head is not None
    return head


def test_init_db_creates_all_tables_at_head():
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)

    tables = set(inspect(engine).get_table_names())
    # Every model's table, plus Alembic's own version bookkeeping.
    assert {"conversations", "messages", "model_endpoints", "model_roles"} <= tables
    assert "alembic_version" in tables


def test_init_db_stamps_the_head_revision():
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)

    with Session(engine) as session:
        stamped = (
            session.connection()
            .execute(text("SELECT version_num FROM alembic_version"))
            .scalar_one()
        )
    assert stamped == _head_revision()


def test_init_db_is_idempotent():
    # Re-running against an already-migrated DB is a no-op, not an error — the
    # startup path runs unconditionally every boot.
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    init_db(engine)
    assert "conversations" in set(inspect(engine).get_table_names())


def test_init_db_reports_orphaned_revision():
    # A dev DB left stamped at a deleted/regenerated migration can't be traced to
    # head; the guard surfaces the DB's revision and the head, not a raw traceback.
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    with Session(engine) as session:
        session.connection().execute(
            text("UPDATE alembic_version SET version_num = 'deadbeef0000'")
        )
        session.commit()

    with pytest.raises(SchemaMigrationError) as exc_info:
        init_db(engine)
    message = str(exc_info.value)
    assert "deadbeef0000" in message  # the orphaned stamp, surfaced verbatim
    assert _head_revision() in message


def test_init_db_reports_schema_drift():
    # Stamp claims head, but a table physically lacks a column the model declares —
    # the `no such column` class. Caught at boot with the table/column named.
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    with Session(engine) as session:
        session.connection().execute(text("ALTER TABLE uploads DROP COLUMN note"))
        session.commit()

    with pytest.raises(SchemaMigrationError) as exc_info:
        init_db(engine)
    message = str(exc_info.value)
    assert "uploads" in message
    assert "note" in message
