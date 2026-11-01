"""The agent's request cap bounds the whole turn, across a park and its resume."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

from pydantic_ai import FunctionToolset, ToolApproved
from pydantic_ai.messages import ModelMessage, ModelRequest, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, DeltaToolCalls, FunctionModel

from agent import build_chat_orchestrator, build_resume_orchestrator
from agent.parked_codec import decode_park, encode_park
from agent.parking import ParkedTurn
from agent.turn import TURN_LIMIT_DETAIL
from runs import Run, RunEnded, RunRegistry, RunStatus
from runs.run import StopCause
from services.conversations import ConversationStore
from tools import RunDeps

from ._agent_helpers import make_stores


def _toolset() -> FunctionToolset[RunDeps]:
    toolset: FunctionToolset[RunDeps] = FunctionToolset()

    @toolset.tool_plain(metadata={"sensitivity": "read"})
    def step(n: int) -> str:
        return f"step {n} done"

    @toolset.tool_plain(requires_approval=True)
    def delete_thing(name: str) -> str:
        return f"deleted {name}"

    return toolset


class _Model:
    """Request 1 does some work, request 2 asks to delete (and parks), and every request
    after the delete has run does more work."""

    def __init__(self) -> None:
        self.requests: int = 0
        self.model: FunctionModel = FunctionModel(stream_function=self._stream)

    async def _stream(
        self, messages: list[ModelMessage], _info: AgentInfo
    ) -> AsyncIterator[str | DeltaToolCalls]:
        self.requests += 1
        returned = {
            part.tool_name
            for message in messages
            if isinstance(message, ModelRequest)
            for part in message.parts
            if isinstance(part, ToolReturnPart)
        }
        if "work_delete_thing" in returned or "work_step" not in returned:
            yield {0: DeltaToolCall(name="work_step", json_args=json.dumps({"n": len(messages)}))}
        else:
            yield {0: DeltaToolCall(name="work_delete_thing", json_args='{"name": "x"}')}


async def _park(tmp_path: Path) -> tuple[RunRegistry, Run, ParkedTurn, _Model, ConversationStore]:
    _, store = await make_stores(tmp_path)
    await store.start()
    conv = await store.create_conversation("operator", title="t")
    model = _Model()
    orchestrator = build_chat_orchestrator(
        "work",
        model=model.model,
        categories={"work": _toolset()},
        store=store,
        conversation_id=conv,
        max_requests=3,
    )
    registry = RunRegistry()
    run = registry.submit(kind="chat", owner_id="operator", orchestrator=orchestrator)
    await run.wait()
    assert run.status is RunStatus.awaiting_input
    parked = run.parked_payload
    assert isinstance(parked, ParkedTurn)
    assert model.requests == 2
    return registry, run, parked, model, store


def _ended(run: Run) -> RunEnded:
    body = run.stream.replay()[-1].body
    assert isinstance(body, RunEnded)
    return body


async def test_a_park_records_the_cap_and_the_requests_spent(tmp_path: Path) -> None:
    _, _, parked, _, _ = await _park(tmp_path)
    assert (parked.max_requests, parked.requests_used) == (3, 2)


async def test_a_live_resume_ends_at_the_cap_after_one_more_request(tmp_path: Path) -> None:
    registry, run, parked, model, store = await _park(tmp_path)
    call_id = parked.requests.approvals[0].tool_call_id

    _ = await registry.resume(
        run.id,
        build_resume_orchestrator(parked, {call_id: ToolApproved()}, store=store),
    )
    await run.wait()

    assert run.status is RunStatus.blocked
    assert run.stop_cause is StopCause.turn_limit
    assert _ended(run).code == "turn_limit"
    assert run.detail == TURN_LIMIT_DETAIL
    # The resumed segment made exactly one request before the cap stopped it.
    assert model.requests == 3


async def test_a_restored_park_keeps_the_cap_and_the_requests_spent(tmp_path: Path) -> None:
    registry, run, parked, model, store = await _park(tmp_path)
    restored, _ = decode_park(encode_park(parked, run), kind="chat")
    assert (restored.max_requests, restored.requests_used) == (3, 2)
    restored.agent = parked.agent
    call_id = restored.requests.approvals[0].tool_call_id

    _ = await registry.resume(
        run.id,
        build_resume_orchestrator(restored, {call_id: ToolApproved()}, store=store),
    )
    await run.wait()

    assert run.status is RunStatus.blocked
    assert _ended(run).code == "turn_limit"
    assert model.requests == 3
