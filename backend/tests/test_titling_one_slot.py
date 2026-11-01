"""Auto-titling against an endpoint that serves one request at a time (LM Studio).

The title call is sent while the answer is still being written, so a one-slot server
queues it behind that answer. The turn must end once its answer is written, and the
title's budget must not be spent waiting in that queue.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models import Model
from pydantic_ai.models.function import AgentInfo, FunctionModel

import agent.naming as naming
from agent import build_chat_orchestrator
from core.config import Settings
from core.db import init_db, make_engine
from core.vault import Vault
from runs import Endpoint, EndpointGate, Run, RunRegistry, RunStatus
from services.conversations import ConversationStore
from services.gated_model import EndpointGatedModel

ANSWER_S = 0.4


class OneSlotServer:
    """Serves one request at a time, in arrival order, like a single-slot local server."""

    def __init__(self) -> None:
        self.slot: asyncio.Lock = asyncio.Lock()
        self.answering: asyncio.Event = asyncio.Event()
        self.answer_done_at: float | None = None

    def answer_model(self) -> FunctionModel:
        async def stream(_messages: list[ModelMessage], _info: AgentInfo) -> AsyncIterator[str]:
            async with self.slot:
                self.answering.set()
                await asyncio.sleep(ANSWER_S)
                yield "the answer"
            self.answer_done_at = time.monotonic()

        return FunctionModel(stream_function=stream)

    def title_model(self, serve_s: float) -> FunctionModel:
        async def title(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
            # Arrives while the answer holds the slot, so it waits behind it.
            _ = await self.answering.wait()
            async with self.slot:
                await asyncio.sleep(serve_s)
            return ModelResponse(parts=[TextPart("Queued Title")])

        return FunctionModel(function=title)


async def _fresh_store(tmp_path: Path) -> ConversationStore:
    engine_ = make_engine("sqlite:///:memory:")
    init_db(engine_)
    vault = Vault(tmp_path / "keyfile.json")
    await vault.setup("pw")
    store = ConversationStore(engine_, vault)
    await store.start()
    return store


async def _first_turn(
    store: ConversationStore,
    conversation_id: str,
    *,
    answer: Model,
    title: Model,
    registry: RunRegistry | None = None,
    endpoint: Endpoint | None = None,
) -> Run:
    orch = build_chat_orchestrator(
        "plan a trip",
        model=answer,
        title_model=title,
        categories={},
        store=store,
        conversation_id=conversation_id,
    )
    run = (registry or RunRegistry()).submit(
        kind="chat",
        owner_id="operator",
        orchestrator=orch,
        conversation_id=conversation_id,
        endpoint=endpoint,
    )
    _ = await run.wait()
    return run


async def _settled_title(run: Run, store: ConversationStore, conversation_id: str) -> str | None:
    await asyncio.wait_for(run.settle_afterwork(), timeout=3)
    summary = await store.get_summary(conversation_id, "operator")
    return summary.title if summary is not None else None


def _title_timeout(monkeypatch: pytest.MonkeyPatch, seconds: float) -> None:
    monkeypatch.setattr(naming, "get_settings", lambda: Settings(title_timeout_s=seconds))


async def test_first_turn_ends_when_its_answer_is_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _title_timeout(monkeypatch, 5.0)
    store = await _fresh_store(tmp_path)
    conv = await store.create_conversation("operator")
    server = OneSlotServer()
    run = await _first_turn(
        store, conv, answer=server.answer_model(), title=server.title_model(serve_s=1.0)
    )
    ended_at = time.monotonic()

    assert run.status is RunStatus.done
    assert server.answer_done_at is not None
    # The title still needs most of a second of the slot; the run does not wait for it.
    assert ended_at - server.answer_done_at < 0.5
    assert await _settled_title(run, store, conv) == "Queued Title"
    await store.stop()


async def test_title_budget_is_not_spent_queued_behind_the_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # The answer holds the slot for longer than the whole title budget; counted from
    # when the title was sent, it would time out before the server ever reached it.
    _title_timeout(monkeypatch, 0.3)
    store = await _fresh_store(tmp_path)
    conv = await store.create_conversation("operator")
    server = OneSlotServer()
    run = await _first_turn(
        store, conv, answer=server.answer_model(), title=server.title_model(serve_s=0.1)
    )

    assert run.status is RunStatus.done
    assert await _settled_title(run, store, conv) == "Queued Title"
    await store.stop()


async def test_title_that_outlives_the_run_is_not_announced_on_its_closed_stream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _title_timeout(monkeypatch, 5.0)
    store = await _fresh_store(tmp_path)
    conv = await store.create_conversation("operator")
    server = OneSlotServer()
    run = await _first_turn(
        store, conv, answer=server.answer_model(), title=server.title_model(serve_s=0.2)
    )
    assert await _settled_title(run, store, conv) == "Queued Title"

    types = [event.body.type for event in run.stream.replay()]
    assert "conversation.titled" not in types
    assert types[-1] == "run.ended"
    await store.stop()


async def test_title_that_never_answers_is_still_bounded_after_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _title_timeout(monkeypatch, 0.2)
    store = await _fresh_store(tmp_path)
    conv = await store.create_conversation("operator")
    server = OneSlotServer()
    run = await _first_turn(
        store, conv, answer=server.answer_model(), title=server.title_model(serve_s=60.0)
    )
    ended_at = time.monotonic()

    assert await _settled_title(run, store, conv) is None
    assert time.monotonic() - ended_at < 1.0
    await store.stop()


async def test_title_takes_its_own_slot_of_a_gated_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # The title must not ride the run's lease: once the run ends, the gate has to count
    # the title's request, or it would hand the endpoint to the next run while the title
    # still occupies the server.
    _title_timeout(monkeypatch, 5.0)
    store = await _fresh_store(tmp_path)
    conv = await store.create_conversation("operator")
    server = OneSlotServer()
    gate = EndpointGate()
    endpoint = Endpoint(id="local", name="local", capacity=1)
    run = await _first_turn(
        store,
        conv,
        answer=EndpointGatedModel(server.answer_model(), gate, endpoint),
        title=EndpointGatedModel(server.title_model(serve_s=0.5), gate, endpoint),
        registry=RunRegistry(endpoints=gate),
        endpoint=endpoint,
    )
    await asyncio.sleep(0.05)

    assert run.afterwork
    assert gate.snapshot(endpoint.id) == (1, 0)
    assert await _settled_title(run, store, conv) == "Queued Title"
    assert gate.snapshot(endpoint.id) == (0, 0)
    await store.stop()


async def test_a_waiting_client_collects_the_title_that_outlived_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _title_timeout(monkeypatch, 5.0)
    store = await _fresh_store(tmp_path)
    conv = await store.create_conversation("operator")
    server = OneSlotServer()
    registry = RunRegistry()
    _ = await _first_turn(
        store,
        conv,
        answer=server.answer_model(),
        title=server.title_model(serve_s=0.3),
        registry=registry,
    )
    summary = await store.get_summary(conv, "operator")
    assert summary is not None and summary.title is None

    await asyncio.wait_for(registry.settle_afterwork(conv, "operator", 3), timeout=3)
    summary = await store.get_summary(conv, "operator")
    assert summary is not None and summary.title == "Queued Title"
    await store.stop()


async def test_waiting_on_a_thread_with_no_afterwork_returns_at_once():
    await asyncio.wait_for(
        RunRegistry().settle_afterwork("no-such-thread", "operator", 30), timeout=0.1
    )
