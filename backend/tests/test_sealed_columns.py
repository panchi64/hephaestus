"""`XC-SEC-3` — the two columns that used to be stored in the clear.

`Conversation.title` is an auto-generated LLM summary of the operator's first message,
the most revealing single line a thread has, and `CorpusSource.path` names the operator's
own filesystem. Both are now sealed. Because the migration that added the sealed columns
runs before unlock (no key), the interesting cases are the transitional ones: a row that
still holds legacy cleartext must read correctly *before* the backfill touches it, and
must have no plaintext left *after*.
"""

from __future__ import annotations

from sqlmodel import Session, SQLModel, col, select

from app import _backfill_sealed_columns
from core.db import in_session
from models.conversation import Conversation
from models.corpus import CorpusSource
from routes.deps import OPERATOR_ID

from ._helpers import client_app


async def _row[M: SQLModel](engine, model_cls: type[M], row_id: str) -> M:
    def work(session: Session) -> M:
        row = session.get(model_cls, row_id)
        assert row is not None
        return row

    return await in_session(engine, work)


async def _write_legacy_conversation(engine, title: str) -> str:
    """A conversation row exactly as the pre-encryption code wrote it: cleartext title,
    no ciphertext. This is what an existing database is full of."""

    def work(session: Session) -> str:
        row = Conversation(owner_id=OPERATOR_ID, title=title)
        session.add(row)
        session.flush()
        return row.id

    return await in_session(engine, work)


# --- new writes are sealed ------------------------------------------------------------


async def test_a_conversation_title_round_trips_sealed():
    async with client_app() as (client, app):
        store = app.state.conversations
        cid = await store.create_conversation(OPERATOR_ID)
        await store.set_title(cid, "Quarterly board deck notes")

        # The read path returns it verbatim…
        summary = await store.get_summary(cid, OPERATOR_ID)
        assert summary.title == "Quarterly board deck notes"
        listed = (await client.get("/conversations")).json()
        assert any(c["title"] == "Quarterly board deck notes" for c in listed)

        # …and nothing readable is on the row.
        row = await _row(app.state.db_engine, Conversation, cid)
        assert row.title is None
        assert row.title_enc is not None
        assert "board" not in row.title_enc


async def test_an_untitled_conversation_stores_no_ciphertext():
    # None is "no title", not "the empty title" — it must not become a sealed blank.
    async with client_app() as (_client, app):
        cid = await app.state.conversations.create_conversation(OPERATOR_ID)
        row = await _row(app.state.db_engine, Conversation, cid)
        assert row.title_enc is None
        assert row.title is None
        assert (await app.state.conversations.get_summary(cid, OPERATOR_ID)).title is None


async def test_a_corpus_folder_path_round_trips_sealed():
    async with client_app() as (client, app):
        created = await client.post("/corpus/folders", json={"path": "/Users/op/clients"})
        assert created.status_code == 201
        assert created.json()["label"] == "/Users/op/clients"

        source_id = created.json()["id"]
        row = await _row(app.state.db_engine, CorpusSource, source_id)
        assert row.path is None
        assert row.path_enc is not None
        assert "clients" not in row.path_enc

        # The listing decrypts it back for the operator.
        sources = (await client.get("/corpus/sources")).json()
        assert any(s["label"] == "/Users/op/clients" for s in sources)


# --- the transitional states ----------------------------------------------------------


async def test_a_legacy_cleartext_title_reads_correctly_before_the_backfill():
    """A half-migrated DB — the sealed column exists but this row predates it. The
    operator must see their thread's real name, not a blank or a garbled one."""
    async with client_app() as (client, app):
        cid = await _write_legacy_conversation(app.state.db_engine, "Old thread")

        assert (await app.state.conversations.get_summary(cid, OPERATOR_ID)).title == "Old thread"
        listed = (await client.get("/conversations")).json()
        assert any(c["id"] == cid and c["title"] == "Old thread" for c in listed)


async def test_the_backfill_seals_a_legacy_title_and_it_still_reads():
    async with client_app() as (_client, app):
        engine = app.state.db_engine
        cid = await _write_legacy_conversation(engine, "Old thread")

        await _backfill_sealed_columns(engine, app.state.vault)

        row = await _row(engine, Conversation, cid)
        assert row.title is None  # the plaintext is gone, not merely ignored
        assert row.title_enc is not None
        # And the same name comes back out.
        assert (await app.state.conversations.get_summary(cid, OPERATOR_ID)).title == "Old thread"


async def test_the_backfill_seals_a_legacy_corpus_path():
    async with client_app() as (client, app):
        engine = app.state.db_engine

        def write(session: Session) -> str:
            row = CorpusSource(owner_id=OPERATOR_ID, path="/srv/notes", status="indexed")
            session.add(row)
            session.flush()
            return row.id

        source_id = await in_session(engine, write)
        # Readable before the heal…
        assert any(s["label"] == "/srv/notes" for s in (await client.get("/corpus/sources")).json())

        await _backfill_sealed_columns(engine, app.state.vault)

        row = await _row(engine, CorpusSource, source_id)
        assert row.path is None
        assert row.path_enc is not None
        # …and readable after it.
        assert any(s["label"] == "/srv/notes" for s in (await client.get("/corpus/sources")).json())


async def test_the_backfill_is_idempotent_and_leaves_sealed_rows_alone():
    async with client_app() as (_client, app):
        engine = app.state.db_engine
        store = app.state.conversations
        sealed_id = await store.create_conversation(OPERATOR_ID)
        await store.set_title(sealed_id, "Already sealed")
        before = (await _row(engine, Conversation, sealed_id)).title_enc

        legacy_id = await _write_legacy_conversation(engine, "Old thread")
        await _backfill_sealed_columns(engine, app.state.vault)
        await _backfill_sealed_columns(engine, app.state.vault)  # a second pass changes nothing

        assert (await _row(engine, Conversation, sealed_id)).title_enc == before
        assert await store.get_summary(legacy_id, OPERATOR_ID) is not None
        assert (await store.get_summary(legacy_id, OPERATOR_ID)).title == "Old thread"


async def test_auto_titling_does_not_clobber_a_legacy_cleartext_name():
    # `set_title_if_absent` must judge "already named" on the effective title, or a
    # pre-encryption thread would silently get re-titled the first time it is used.
    async with client_app() as (_client, app):
        store = app.state.conversations
        cid = await _write_legacy_conversation(app.state.db_engine, "Named by the operator")
        assert await store.set_title_if_absent(cid, "A fresh auto-title") is False
        assert (await store.get_summary(cid, OPERATOR_ID)).title == "Named by the operator"


async def test_auto_titling_fills_and_seals_an_unnamed_thread():
    async with client_app() as (_client, app):
        store = app.state.conversations
        cid = await store.create_conversation(OPERATOR_ID)
        assert await store.set_title_if_absent(cid, "A fresh auto-title") is True

        row = await _row(app.state.db_engine, Conversation, cid)
        assert row.title is None
        assert row.title_enc is not None
        assert (await store.get_summary(cid, OPERATOR_ID)).title == "A fresh auto-title"


async def test_no_cleartext_titles_remain_after_a_boot_with_a_mixed_database():
    """The end state the requirement asks for: nothing readable left in the column."""
    async with client_app() as (_client, app):
        engine = app.state.db_engine
        for name in ("one", "two", "three"):
            await _write_legacy_conversation(engine, name)
        await app.state.conversations.set_title(
            await app.state.conversations.create_conversation(OPERATOR_ID), "four"
        )

        await _backfill_sealed_columns(engine, app.state.vault)

        def remaining(session: Session) -> list[str | None]:
            rows = session.exec(
                select(Conversation).where(col(Conversation.title).is_not(None))
            ).all()
            return [r.title for r in rows]

        assert await in_session(engine, remaining) == []
