"""core.db engine configuration: the per-connection PRAGMAs `make_engine` installs."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import Connection, CursorResult, text
from sqlalchemy.exc import OperationalError
from sqlmodel import Session

from core.db import BUSY_TIMEOUT_MS, LOCK_RETRIES, get_owned, in_session, init_db, make_engine
from core.exceptions import NotFoundError
from models.memory import Memory


def _locked() -> OperationalError:
    """SQLite's writer-lock error, as SQLAlchemy wraps it."""
    return OperationalError("UPDATE …", {}, Exception("database is locked"))


def _pragma(connection: Connection, name: str) -> int:
    result: CursorResult[int] = connection.execute(text(f"PRAGMA {name}"))
    return result.scalar_one()


def test_file_backed_connections_get_the_busy_timeout(tmp_path: Path):
    """The connect handler must arm SQLite's busy handler on every pooled connection —
    without it, two threadpool sessions colliding on the single write lock turn a
    microseconds-long overlap into an immediate `database is locked` (the scheduler
    double-fire regression's root cause)."""
    engine = make_engine(f"sqlite:///{tmp_path / 'app.db'}")
    with engine.connect() as connection:
        value = _pragma(connection, "busy_timeout")
    assert value == BUSY_TIMEOUT_MS

    # The handler fires per *connect*, not once per engine — a genuinely fresh
    # DBAPI connection (the pool's is discarded) must come armed the same way.
    engine.dispose()
    with engine.connect() as connection:
        value = _pragma(connection, "busy_timeout")
    assert value == BUSY_TIMEOUT_MS


def test_foreign_keys_are_on_per_connection(tmp_path: Path):
    engine = make_engine(f"sqlite:///{tmp_path / 'app.db'}")
    with engine.connect() as connection:
        assert _pragma(connection, "foreign_keys") == 1


# --- the owner-scoped single-row read every store shares ----------------------
async def test_get_owned_answers_identically_for_missing_and_foreign_rows():
    # The whole point of the ownership check: distinguishing "not yours" from "doesn't
    # exist" confirms to a caller that an id it guessed is real. Every store's read of a
    # single row crosses this boundary, so the two must be indistinguishable.
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)

    def insert(session: Session) -> str:
        memory = Memory(owner_id="someone-else", content_enc="x")
        session.add(memory)
        session.flush()
        return memory.id

    foreign_id = await in_session(engine, insert)

    with pytest.raises(NotFoundError) as foreign:
        _ = await get_owned(engine, Memory, foreign_id, "operator", what="memory")
    with pytest.raises(NotFoundError) as missing:
        _ = await get_owned(engine, Memory, "no-such-id", "operator", what="memory")

    assert str(foreign.value) == f"memory {foreign_id!r} not found"
    assert str(missing.value) == "memory 'no-such-id' not found"


async def test_get_owned_returns_the_row_for_its_owner():
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)

    def insert(session: Session) -> str:
        memory = Memory(owner_id="operator", content_enc="x")
        session.add(memory)
        session.flush()
        return memory.id

    memory_id = await in_session(engine, insert)

    row = await get_owned(engine, Memory, memory_id, "operator", what="memory")
    assert row.id == memory_id and row.owner_id == "operator"


class TestLosingTheWriteLock:
    """SQLite admits one writer, and the busy handler is a budget rather than a promise.

    This is the failure mode nothing in the suite would otherwise see: every test runs
    against `:memory:`, where there is one connection and no contention, while the product
    runs two agent turns at once against a file — routine now that the agent launches
    sub-agents — with the write-behind drainers alongside them. What that produced was a
    hard `database is locked` on an ordinary bookkeeping update.
    """

    async def test_a_locked_write_is_tried_again(self):
        engine = make_engine("sqlite:///:memory:")
        init_db(engine)
        attempts = 0

        def work(_session: Session) -> str:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise _locked()
            return "written"

        assert await in_session(engine, work) == "written"
        assert attempts == 2

    async def test_it_gives_up_rather_than_retrying_forever(self):
        engine = make_engine("sqlite:///:memory:")
        init_db(engine)
        attempts = 0

        def work(_session: Session) -> None:
            nonlocal attempts
            attempts += 1
            raise _locked()

        with pytest.raises(OperationalError):
            await in_session(engine, work)
        # A caller blocked forever on a lock nobody is releasing is worse than one told.
        assert attempts == LOCK_RETRIES + 1

    async def test_a_real_fault_is_raised_at_once(self):
        engine = make_engine("sqlite:///:memory:")
        init_db(engine)
        attempts = 0

        def work(_session: Session) -> None:
            nonlocal attempts
            attempts += 1
            raise OperationalError("SELECT …", {}, Exception("no such column: nope"))

        with pytest.raises(OperationalError):
            await in_session(engine, work)
        # Retrying a missing column turns a fast, legible failure into a slow one.
        assert attempts == 1
