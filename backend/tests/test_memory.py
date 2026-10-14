"""Long-term memory: hybrid recall, keyword fallback, encryption, REST."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import override

import pytest
from sqlalchemy import Engine

from core.db import init_db, make_engine
from core.exceptions import DegradedCapabilityError, NotFoundError
from core.vault import Vault
from services.embeddings import Embedder, EmbeddingBatch
from services.memory import MemoryStore

from ._helpers import client_app
from ._json import json_body, json_list, obj

OWNER = "operator"

# A tiny concept space so paraphrases (no shared tokens) still embed alike —
# lets the dense path be tested independently of keyword overlap.
_CONCEPTS = {
    "cat": 0,
    "feline": 0,
    "kitten": 0,
    "dog": 1,
    "canine": 1,
    "puppy": 1,
    "car": 2,
    "vehicle": 2,
    "automobile": 2,
}


class FakeEmbedder(Embedder):
    def __init__(self, model: str = "fake-embed", dim: int = 4) -> None:
        self._model: str = model
        self._dim: int = dim

    @override
    async def is_available(self, owner_id: str) -> bool:
        return True

    @override
    async def embed(self, owner_id: str, texts: list[str]) -> EmbeddingBatch:
        vectors: list[list[float]] = []
        for text in texts:
            vec = [0.0] * self._dim
            for raw in text.lower().split():
                token = "".join(ch for ch in raw if ch.isalnum())
                if token in _CONCEPTS:
                    vec[_CONCEPTS[token]] += 1.0
            vectors.append(vec)
        return EmbeddingBatch(vectors=vectors, model=self._model, dim=self._dim)


class DegradedEmbedder(Embedder):
    @override
    async def is_available(self, owner_id: str) -> bool:
        return False

    @override
    async def embed(self, owner_id: str, texts: list[str]) -> EmbeddingBatch:
        raise DegradedCapabilityError("no embedding endpoint configured")


def _store(embedder: Embedder) -> tuple[Engine, Vault, MemoryStore]:
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    vault = Vault(Path(tempfile.mkdtemp()) / "keyfile.json")
    return engine, vault, MemoryStore(engine, vault, embedder)


async def _setup(embedder: Embedder) -> MemoryStore:
    _engine, vault, store = _store(embedder)
    await vault.setup("pw")
    return store


# --- recall ---------------------------------------------------------------


async def test_semantic_recall_matches_paraphrase():
    store = await _setup(FakeEmbedder())
    _ = await store.remember(OWNER, "I have a pet cat at home")
    _ = await store.remember(OWNER, "My commute uses a car")

    # "feline" shares no tokens with "cat" — only the dense path can match it.
    hits = await store.recall(OWNER, "feline", limit=5)
    assert hits, "semantic recall should find the cat memory"
    top = hits[0]
    assert "cat" in top.memory.content
    assert top.matched_by == "semantic"


async def test_keyword_recall_matches_exact_token():
    store = await _setup(FakeEmbedder())
    _ = await store.remember(OWNER, "My AWS account id is 998877")

    # A rare exact token embeddings would miss — the lexical path catches it.
    hits = await store.recall(OWNER, "998877", limit=5)
    assert hits
    assert hits[0].matched_by == "keyword"


async def test_recall_degrades_to_keyword_without_embeddings():
    store = await _setup(DegradedEmbedder())
    _ = await store.remember(OWNER, "The wifi password is hunter2")

    # No embedder → vectors never stored, query can't embed → keyword only (MEM-2).
    hits = await store.recall(OWNER, "wifi password", limit=5)
    assert hits
    assert hits[0].matched_by == "keyword"
    # And nothing was stored as a vector.
    views = await store.list_memories(OWNER)
    assert all(not v.has_embedding for v in views)


async def test_pinned_memory_always_recalled():
    store = await _setup(FakeEmbedder())
    _ = await store.remember(OWNER, "unrelated note about a dog", pinned=True)
    _ = await store.remember(OWNER, "a memory about a car")

    hits = await store.recall(OWNER, "automobile", limit=5)
    contents = [h.memory.content for h in hits]
    assert any("dog" in c for c in contents)  # pinned, despite no relevance
    assert any(h.matched_by == "pinned" for h in hits)


async def test_embedding_model_change_is_segregated():
    # A memory embedded by one model is not dense-compared against another's
    # query vector (EMB-2) — it can still surface via keyword, not via meaning.
    store = await _setup(FakeEmbedder(model="model-a"))
    _ = await store.remember(OWNER, "I love my cat")
    store.embedder = FakeEmbedder(model="model-b")  # operator changed the model

    hits = await store.recall(OWNER, "feline", limit=5)
    # Different space → no dense match, and no token overlap → no recall at all.
    assert all(h.matched_by != "semantic" for h in hits)


async def test_reembed_heals_a_model_change():
    # The inverse of segregation: after the operator switches embedding models,
    # reembed lifts every memory into the new space so meaning-recall works again.
    store = await _setup(FakeEmbedder(model="model-a"))
    _ = await store.remember(OWNER, "I love my cat")
    store.embedder = FakeEmbedder(model="model-b")  # operator changed the model

    before = await store.recall(OWNER, "feline", limit=5)
    assert all(h.matched_by != "semantic" for h in before)  # segregated

    count = await store.reembed(OWNER, current_model="model-b")
    assert count == 1
    after = await store.recall(OWNER, "feline", limit=5)
    assert after and after[0].matched_by == "semantic"  # healed: dense matches again


# --- CRUD + encryption ----------------------------------------------------


async def test_crud_and_timeline_order():
    store = await _setup(FakeEmbedder())
    a = await store.remember(OWNER, "first")
    b = await store.remember(OWNER, "second")

    views = await store.list_memories(OWNER)
    assert [v.id for v in views] == [b.id, a.id]  # newest first

    _ = await store.update(OWNER, a.id, content="first edited", pinned=True)
    got = await store.get(OWNER, a.id)
    assert got.content == "first edited" and got.pinned is True

    _ = await store.delete(OWNER, b.id)
    assert [v.id for v in await store.list_memories(OWNER)] == [a.id]


async def test_content_and_vector_encrypted_at_rest():
    engine, vault, store = _store(FakeEmbedder())
    await vault.setup("pw")
    _ = await store.remember(OWNER, "cat secret")

    # Read the raw row: neither content nor embedding is stored in the clear.
    from sqlmodel import Session, select

    from models.memory import Memory

    with Session(engine) as session:
        row = session.exec(select(Memory)).one()
    assert "cat secret" not in row.content_enc
    assert row.embedding_enc is not None and "1.0" not in row.embedding_enc


async def test_audit_flags_near_duplicates():
    store = await _setup(FakeEmbedder())
    _ = await store.remember(OWNER, "I have a cat")
    _ = await store.remember(OWNER, "my kitten is here")  # same concept → near-dup
    _ = await store.remember(OWNER, "I drive a car")

    groups = await store.audit(OWNER)
    assert len(groups) == 1
    assert len(groups[0].memory_ids) == 2


async def test_get_unknown_memory_is_not_found():
    store = await _setup(FakeEmbedder())
    with pytest.raises(NotFoundError):
        _ = await store.get(OWNER, "nope")


# --- REST surface ---------------------------------------------------------


async def test_memory_rest_crud_and_keyword_recall():
    # No embedding endpoint is configured in the booted app, so recall exercises
    # the keyword-fallback path end to end over HTTP.
    async with client_app() as (client, _app):
        created = await client.post("/memory", json={"content": "my gate code is 4455"})
        assert created.status_code == 201
        body = json_body(created)
        assert body["content"] == "my gate code is 4455"
        assert body["has_embedding"] is False  # degraded embedder, no vector
        memory_id = body["id"]

        listing = json_list(await client.get("/memory"))
        assert [obj(m)["id"] for m in listing] == [memory_id]

        recall = await client.post("/memory/recall", json={"query": "4455"})
        assert recall.status_code == 200
        hits = json_list(recall)
        assert hits and obj(hits[0])["matched_by"] == "keyword"

        patched = await client.patch(f"/memory/{memory_id}", json={"pinned": True})
        assert json_body(patched)["pinned"] is True

        deleted = await client.delete(f"/memory/{memory_id}")
        assert deleted.status_code == 204
        assert json_list(await client.get("/memory")) == []


async def test_memory_rest_unknown_id_404():
    async with client_app() as (client, _app):
        assert (await client.get("/memory/nope")).status_code == 404
        assert (await client.delete("/memory/nope")).status_code == 404


# --- agent reaches the capability through the toolset stack ----------------


async def test_agent_memory_tool_reaches_the_store():
    # A turn with only the memory category and a TestModel (which calls every
    # offered tool once) must drive the remember tool through to the service —
    # proving the tool is a thin adapter over the same MemoryStore.
    from pydantic_ai.models.test import TestModel

    from agent import build_chat_orchestrator
    from core.container import ServiceContainer
    from runs import RunRegistry, RunStatus
    from tools.memory import memory_toolset

    store = await _setup(FakeEmbedder())
    # Neither verb gates: recall reads the operator's own notes and remember writes them,
    # so the turn drives both through to the store without an approval anywhere.
    orch = build_chat_orchestrator(
        "note something",
        model=TestModel(custom_output_text="done"),
        categories={"memory": memory_toolset()},
        capabilities=ServiceContainer.of(store),
        conversation_id="c",
    )
    run = RunRegistry().submit(kind="chat", owner_id=OWNER, orchestrator=orch)
    await run.wait()

    assert run.status is RunStatus.done
    assert await store.list_memories(OWNER), "the remember tool should have stored a memory"
