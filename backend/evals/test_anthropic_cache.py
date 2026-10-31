"""Live check that a Claude thread caches across turns: each turn reads the previous
turn's whole prefix and Anthropic's cache diagnostics find no divergence, including
across a permission-level change. Spends real money: three short turns on the full
tool catalog."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from pydantic_ai import ModelResponse

from agent import build_chat_orchestrator
from core.db import init_db, make_engine
from core.vault import Vault
from runs import RunRegistry
from services import llm
from services.conversations import ConversationBinding, ConversationStore, last_request_usage
from services.endpoint_spec import EndpointSpec
from services.permissions import PermissionLevel
from tools import core_categories

REQUIRED_ENV = ("ANTHROPIC_API_KEY", "HEPH_EVAL_ANTHROPIC_MODEL")
OWNER = "operator"


@pytest.fixture(autouse=True)
async def _preflight() -> None:
    """Replaces the directory's embedding preflight; this file uses no embedder."""


async def test_each_turn_reads_the_previous_prefix(tmp_path: Path):
    model = llm.build_model(
        EndpointSpec(
            base_url="https://api.anthropic.com",
            model=os.environ["HEPH_EVAL_ANTHROPIC_MODEL"],
            provider="anthropic",
            api_key=os.environ["ANTHROPIC_API_KEY"],
        )
    )
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    vault = Vault(tmp_path / "keyfile.json")
    await vault.setup("pw")
    store = ConversationStore(engine, vault)
    await store.start()
    conversation_id = await store.create_conversation(OWNER)
    registry = RunRegistry()

    reads: list[int] = []
    # Manual rather than Plan for the level change: Plan withholds tools, which rewrites
    # the tool array by design, where Manual changes only the level's instruction block.
    turns: tuple[tuple[str, PermissionLevel], ...] = (
        ("Say hi in three words.", "auto"),
        ("Now say bye in three words.", "auto"),
        ("Say ok.", "manual"),
    )
    for prompt, level in turns:
        orchestrator = build_chat_orchestrator(
            prompt,
            model=model,
            categories=core_categories(),
            store=store,
            conversation_id=conversation_id,
            binding=ConversationBinding(permission=level),
        )
        run = registry.submit(kind="chat", owner_id=OWNER, orchestrator=orchestrator)
        await run.wait()
        history = await store.history(conversation_id)
        last = last_request_usage(history)
        assert last is not None
        assert last.cache_miss_reason is None, last
        response = next(m for m in reversed(history) if isinstance(m, ModelResponse))
        reads.append(response.usage.cache_read_tokens)
    await store.stop()

    # Turn 1 writes the tools and the brief; every later turn reads at least that much.
    assert reads[1] > 0 and reads[2] >= reads[1], reads
