"""Cross-chat search: hybrid recall over other conversations, read, encryption."""

from __future__ import annotations

import tempfile
from pathlib import Path

from pydantic_ai import ModelMessage, ModelRequest, ModelResponse, TextPart, UserPromptPart
from sqlmodel import Session, select

from core.db import init_db, make_engine
from core.exceptions import DegradedCapabilityError
from core.vault import Vault
from models.conversation import Message
from services.conversation_search import ConversationSearch
from services.conversations import ConversationStore
from services.embeddings import EmbeddingBatch

OWNER = "operator"

# A tiny concept space so paraphrases (no shared tokens) still embed alike — lets
# the dense path be tested independently of keyword overlap (mirrors test_memory).
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


class FakeEmbedder:
    def __init__(self, model: str = "fake-embed", dim: int = 4) -> None:
        self._model = model
        self._dim = dim

    async def is_available(self, owner_id: str) -> bool:
        return True

    async def embed(self, owner_id: str, texts: list[str]) -> EmbeddingBatch:
        vectors = []
        for text in texts:
            vec = [0.0] * self._dim
            for raw in text.lower().split():
                token = "".join(ch for ch in raw if ch.isalnum())
                if token in _CONCEPTS:
                    vec[_CONCEPTS[token]] += 1.0
            vectors.append(vec)
        return EmbeddingBatch(vectors=vectors, model=self._model, dim=self._dim)


class DegradedEmbedder:
    async def is_available(self, owner_id: str) -> bool:
        return False

    async def embed(self, owner_id: str, texts: list[str]) -> EmbeddingBatch:
        raise DegradedCapabilityError("no embedding endpoint configured")


class BrokenEmbedder:
    """Fails with a non-degradation error — e.g. a network timeout or a 5xx."""

    async def is_available(self, owner_id: str) -> bool:
        return True

    async def embed(self, owner_id: str, texts: list[str]) -> EmbeddingBatch:
        raise RuntimeError("embedding endpoint exploded")


def _turn(prompt: str, answer: str) -> list[ModelMessage]:
    return [
        ModelRequest(parts=[UserPromptPart(content=prompt)]),
        ModelResponse(parts=[TextPart(content=answer)], model_name="m"),
    ]


async def _setup(embedder):
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    vault = Vault(Path(tempfile.mkdtemp()) / "keyfile.json")
    await vault.setup("pw")
    store = ConversationStore(engine, vault, embedder)
    await store.start()
    search = ConversationSearch(engine, vault, embedder, store)
    return engine, vault, store, search


async def _record(store, owner: str, *turns, ephemeral: bool = False) -> str:
    cid = await store.create_conversation(owner, ephemeral=ephemeral)
    for prompt, answer in turns:
        store.record(cid, _turn(prompt, answer))
    await store._worker.join()  # flush the drainer so the rows (and vectors) land
    return cid


# --- search ---------------------------------------------------------------


async def test_semantic_search_matches_paraphrase():
    _engine, _vault, store, search = await _setup(FakeEmbedder())
    cat = await _record(store, OWNER, ("tell me about my cat", "your cat is fluffy"))
    await _record(store, OWNER, ("my commute", "you drive a car"))

    # "feline" shares no tokens with "cat" — only the dense path can match it.
    hits = await search.search(OWNER, "feline", limit=5)
    assert hits, "semantic search should find the cat conversation"
    assert hits[0].conversation_id == cat
    assert hits[0].matched_by == "semantic"
    await store.stop()


async def test_search_hit_title_is_none_for_an_untitled_conversation():
    # A conversation the background titler hasn't named yet surfaces title=None from
    # the service — the corpus adapter fills a fallback so the model is never handed a
    # null (see services/corpus/wrappers.py).
    _engine, _vault, store, search = await _setup(FakeEmbedder())
    cid = await _record(store, OWNER, ("about a cat", "a fluffy cat"))

    hits = await search.search(OWNER, "feline", limit=5)
    assert hits and hits[0].conversation_id == cid
    assert hits[0].title is None
    await store.stop()


async def test_keyword_search_matches_exact_token():
    _engine, _vault, store, search = await _setup(FakeEmbedder())
    cid = await _record(store, OWNER, ("note", "the gate code is 998877"))

    # A rare token embeddings would miss — the lexical path catches it.
    hits = await search.search(OWNER, "998877", limit=5)
    assert hits and hits[0].conversation_id == cid
    assert hits[0].matched_by == "keyword"
    assert "998877" in hits[0].snippet
    await store.stop()


async def test_search_excludes_the_current_conversation():
    _engine, _vault, store, search = await _setup(FakeEmbedder())
    current = await _record(store, OWNER, ("about cats", "a cat is here"))
    other = await _record(store, OWNER, ("also cats", "another cat"))

    hits = await search.search(OWNER, "feline", exclude_conversation_id=current)
    ids = {h.conversation_id for h in hits}
    assert other in ids and current not in ids
    await store.stop()


async def test_search_degrades_to_keyword_without_embeddings():
    _engine, _vault, store, search = await _setup(DegradedEmbedder())
    cid = await _record(store, OWNER, ("note", "the wifi password is hunter2"))

    hits = await search.search(OWNER, "wifi password", limit=5)
    assert hits and hits[0].conversation_id == cid
    assert hits[0].matched_by == "keyword"
    await store.stop()


async def test_search_excludes_ephemeral_and_other_owners():
    _engine, _vault, store, search = await _setup(FakeEmbedder())
    scratch = await _record(store, OWNER, ("compare", "a cat"), ephemeral=True)
    foreign = await _record(store, "intruder", ("theirs", "a cat"))
    mine = await _record(store, OWNER, ("mine", "a cat"))

    ids = {h.conversation_id for h in await search.search(OWNER, "feline", limit=10)}
    assert mine in ids
    assert scratch not in ids and foreign not in ids
    await store.stop()


async def test_search_empty_corpus_returns_empty():
    _engine, _vault, store, search = await _setup(FakeEmbedder())
    assert await search.search(OWNER, "anything") == []
    await store.stop()


# --- read -----------------------------------------------------------------


async def test_read_returns_transcript_for_owner():
    _engine, _vault, store, search = await _setup(FakeEmbedder())
    cid = await _record(store, OWNER, ("what is 2+2", "it is 4"))

    transcript = await search.read(OWNER, cid)
    assert transcript is not None
    assert "User: what is 2+2" in transcript.text
    assert "Assistant: it is 4" in transcript.text
    await store.stop()


async def test_read_foreign_owner_returns_none():
    _engine, _vault, store, search = await _setup(FakeEmbedder())
    cid = await _record(store, "intruder", ("secret", "hidden"))
    assert await search.read(OWNER, cid) is None
    await store.stop()


# --- indexing on write ----------------------------------------------------


async def test_persisted_text_is_embedded_and_encrypted():
    engine, _vault, store, _search = await _setup(FakeEmbedder())
    # A content-bearing request + an empty-text response (e.g. a tool-only turn).
    cid = await store.create_conversation(OWNER)
    store.record(
        cid,
        [
            ModelRequest(parts=[UserPromptPart(content="about a cat")]),
            ModelResponse(parts=[TextPart(content="")], model_name="m"),
        ],
    )
    await store._worker.join()

    with Session(engine) as session:
        rows = session.exec(select(Message).where(Message.conversation_id == cid)).all()
    by_kind = {row.kind: row for row in rows}
    # The request carried text → embedded; its vector is encrypted at rest.
    assert by_kind["request"].embedding_enc is not None
    assert "1.0" not in by_kind["request"].embedding_enc
    # The empty response has no searchable text → no vector.
    assert by_kind["response"].embedding_enc is None
    await store.stop()


async def test_degraded_embedder_still_persists_messages():
    engine, _vault, store, search = await _setup(DegradedEmbedder())
    cid = await _record(store, OWNER, ("hello", "world"))

    with Session(engine) as session:
        rows = session.exec(select(Message).where(Message.conversation_id == cid)).all()
    assert rows and all(row.embedding_enc is None for row in rows)
    # The conversation is still keyword-searchable despite no vectors.
    assert await search.search(OWNER, "world")
    await store.stop()


async def test_embedder_error_never_loses_the_message():
    # A non-degradation embedder failure (timeout, 5xx) must NOT crash the drainer
    # or drop the persist job — the turn is written, just without a vector.
    engine, _vault, store, search = await _setup(BrokenEmbedder())
    cid = await _record(store, OWNER, ("hello there", "general kenobi"))

    with Session(engine) as session:
        rows = session.exec(select(Message).where(Message.conversation_id == cid)).all()
    assert len(rows) == 2  # the turn persisted despite the embedder raising
    assert all(row.embedding_enc is None for row in rows)
    # And it remains keyword-searchable.
    assert await search.search(OWNER, "kenobi")
    await store.stop()


async def test_backfill_embeds_pending_messages():
    # Persisted while the embedder was down (no vectors), then an endpoint appears.
    _engine, _vault, store, _search = await _setup(DegradedEmbedder())
    cid = await _record(store, OWNER, ("about a cat", "a fluffy cat"))

    store._embedder = FakeEmbedder()  # operator configured an embedding endpoint
    embedded = await store.backfill_embeddings(OWNER)
    assert embedded == 2  # both content-bearing messages

    # Now the dense path finds the conversation by meaning, not just keywords.
    search = ConversationSearch(store._engine, store._vault, FakeEmbedder(), store)
    hits = await search.search(OWNER, "feline", limit=5)
    assert hits and hits[0].conversation_id == cid
    assert hits[0].matched_by == "semantic"
    await store.stop()


async def test_reindex_heals_a_model_change():
    # After the operator switches embedding models, reindex re-embeds the stale
    # vectors into the new space so cross-chat meaning-search works again.
    _engine, _vault, store, search = await _setup(FakeEmbedder(model="model-a"))
    cid = await _record(store, OWNER, ("about pets", "I have a cat"))

    store._embedder = FakeEmbedder(model="model-b")  # operator changed the model
    search._embedder = FakeEmbedder(model="model-b")
    before = await search.search(OWNER, "feline", limit=5)
    assert all(h.matched_by != "semantic" for h in before)  # segregated

    count = await store.reindex_embeddings(OWNER, current_model="model-b")
    assert count == 2  # both content-bearing turns were on the stale model
    after = await search.search(OWNER, "feline", limit=5)
    assert after and after[0].conversation_id == cid
    assert after[0].matched_by == "semantic"  # healed
    await store.stop()
