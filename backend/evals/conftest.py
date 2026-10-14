"""Eval gating + live fixtures.

Registers the ``live_models`` marker and **auto-skips every test in this directory
unless all six ``HEPH_EVAL_*`` env vars are set**, so the default ``uv run pytest``
(no creds) never runs these and stays green. Provides the live embedder + chat
model fixtures, and a load-bearing **preflight** that does one real
``embed(["probe"])`` and asserts a non-empty vector of positive dim.

Why the preflight is load-bearing: nothing in the stack validates that the
``embedding`` role points at a real embeddings model — a wrong/missing endpoint
degrades to keyword-only recall *silently*. Without this gate, a degraded
keyword-only run could be mistaken for the embeddings' true quality. The preflight
makes that failure loud (a hard error) instead of a quietly-wrong number.
"""

from __future__ import annotations

import tempfile
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from core.db import init_db, make_engine
from core.vault import Vault
from evals.live import EnvEmbedder, build_chat_model, missing_env
from services.conversation_search import ConversationSearch
from services.conversations import ConversationStore
from services.memory import MemoryStore

OWNER = "operator"


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "live_models: requires the six HEPH_EVAL_* env vars (a live embedding + chat "
        + "endpoint); auto-skipped otherwise.",
    )


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Mark every test in this dir ``live_models`` and skip the lot when the env is
    absent — so the suite is invisible to a credential-free ``uv run pytest``."""
    absent = missing_env()
    skip = pytest.mark.skip(
        reason=f"live model evals: set {', '.join(absent)} to run" if absent else ""
    )
    for item in items:
        item.add_marker(pytest.mark.live_models)
        if absent:
            item.add_marker(skip)


@pytest.fixture
def embedder() -> EnvEmbedder:
    """The live env-backed embedder under test."""
    return EnvEmbedder.from_env()


@pytest.fixture
def chat_model():
    """A single Pydantic AI chat model built from the env endpoint."""
    return build_chat_model()


@pytest.fixture(autouse=True)
async def _preflight(embedder: EnvEmbedder) -> None:
    """One live embed call before any measurement — fail loudly if the endpoint is
    not actually returning vectors, so a degraded keyword-only run can't masquerade
    as real embedding quality."""
    batch = await embedder.embed(OWNER, ["probe"])
    assert batch.vectors, "preflight: embedding endpoint returned no vectors"
    assert batch.dim > 0, "preflight: embedding endpoint returned a zero-dim vector"
    assert len(batch.vectors[0]) == batch.dim, "preflight: vector length disagrees with dim"


async def _unlocked_vault() -> Vault:
    """A fresh keyfile-backed vault, unlocked — kept off the real ``data/`` dir."""
    vault = Vault(Path(tempfile.mkdtemp()) / "keyfile.json")
    await vault.setup("pw")
    return vault


@pytest.fixture
async def memory_store(embedder: EnvEmbedder) -> MemoryStore:
    """A live-embedder ``MemoryStore`` on a throwaway in-memory DB."""
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    vault = await _unlocked_vault()
    return MemoryStore(engine, vault, embedder)


@pytest.fixture
async def conversation_search(
    embedder: EnvEmbedder,
) -> AsyncIterator[tuple[ConversationStore, ConversationSearch]]:
    """A live-embedder conversation store + its cross-chat search, on a throwaway DB.

    The drainer is started so seeded turns embed through the real persistence path;
    it is stopped on teardown."""
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    vault = await _unlocked_vault()
    store = ConversationStore(engine, vault, embedder)
    await store.start()
    search = ConversationSearch(engine, vault, embedder, store)
    try:
        yield store, search
    finally:
        await store.stop()
