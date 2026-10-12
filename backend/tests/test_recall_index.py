"""The memory-only recall index: ranks without re-decrypting, stays current by diffing."""

from __future__ import annotations

import tempfile
from pathlib import Path

from sqlmodel import Session, select

from core.db import init_db, make_engine
from core.vault import Vault
from models.memory import Memory
from services.memory import MemoryStore
from services.recall_index import rank_texts

from .test_memory import DegradedEmbedder, FakeEmbedder

OWNER = "operator"


class CountingVault(Vault):
    def __init__(self, keyfile: Path) -> None:
        super().__init__(keyfile)
        self.opened = 0

    def decrypt_str(self, token: str) -> str:
        self.opened += 1
        return super().decrypt_str(token)


async def _store(embedder=None):
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    vault = CountingVault(Path(tempfile.mkdtemp()) / "keyfile.json")
    await vault.setup("pw")
    return engine, vault, MemoryStore(engine, vault, embedder or FakeEmbedder())


async def test_a_repeated_search_decrypts_only_what_it_returns():
    _engine, vault, store = await _store()
    for i in range(30):
        await store.remember(OWNER, f"note {i} about the garden")
    await store.recall(OWNER, "garden", limit=3)
    vault.opened = 0
    hits = await store.recall(OWNER, "garden", limit=3)
    assert len(hits) == 3
    assert vault.opened == 3


async def test_a_new_row_is_found_and_only_it_is_decrypted():
    _engine, vault, store = await _store()
    for i in range(10):
        await store.remember(OWNER, f"note {i} about the garden")
    await store.recall(OWNER, "garden")
    await store.remember(OWNER, "the boiler needs servicing")
    vault.opened = 0
    hits = await store.recall(OWNER, "boiler", limit=1)
    assert [hit.memory.content for hit in hits] == ["the boiler needs servicing"]
    # Its text and vector to index it, its text again to return it.
    assert vault.opened == 3


async def test_an_edited_row_is_reindexed_and_a_deleted_one_drops_out():
    _engine, _vault, store = await _store(DegradedEmbedder())
    kept = await store.remember(OWNER, "the boiler needs servicing")
    gone = await store.remember(OWNER, "the boiler is in the cellar")
    assert len(await store.recall(OWNER, "boiler")) == 2

    await store.update(OWNER, kept.id, content="the kettle needs descaling")
    await store.delete(OWNER, gone.id)
    assert await store.recall(OWNER, "boiler") == []
    assert [hit.memory.id for hit in await store.recall(OWNER, "kettle")] == [kept.id]


async def test_a_row_changed_behind_the_store_is_still_picked_up():
    # A write path that never tells the index anything cannot leave it stale.
    engine, _vault, store = await _store(DegradedEmbedder())
    await store.remember(OWNER, "first note")
    await store.recall(OWNER, "first")
    with Session(engine) as session:
        session.delete(session.exec(select(Memory)).one())
        session.commit()
    assert await store.recall(OWNER, "first") == []


async def test_locking_the_vault_drops_the_index():
    _engine, vault, store = await _store()
    await store.remember(OWNER, "the garden gate sticks")
    await store.recall(OWNER, "garden")
    assert store._index._shards
    vault.lock()
    assert not store._index._shards


async def test_meaning_still_ranks_through_the_index():
    _engine, _vault, store = await _store()
    await store.remember(OWNER, "my cat is fluffy")
    await store.remember(OWNER, "the car is red")
    hits = await store.recall(OWNER, "feline")
    assert hits[0].memory.content == "my cat is fluffy"
    assert hits[0].matched_by == "semantic"


def test_rarer_terms_weigh_more():
    ranked = rank_texts(
        {"a": "deploy the service", "b": "deploy the service with kubernetes", "c": "deploy"},
        {"deploy", "kubernetes"},
    )
    assert ranked[0][0] == "b"
