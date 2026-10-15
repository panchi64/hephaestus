"""A turn that is getting somewhere carries on past the request limit by itself, and a
request a transient model error cut short is sent again.

Both resume from the last complete request in the same run, so the operator's thread reads
as one turn. Neither is unconditional: an extension needs a completed tool call since the
last one, and both have a budget.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import pytest
from pydantic_ai import FunctionToolset
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelMessage, ModelRequest, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, DeltaToolCalls, FunctionModel

import agent.engine as engine
import agent.turn as turn
from agent import build_chat_orchestrator
from core.config import Settings
from runs import LimitNotice, RunRegistry, RunStatus
from tools import RunDeps

from ._helpers import bodies_of


def _work_toolset(*, fail: bool = False) -> FunctionToolset[RunDeps]:
    toolset: FunctionToolset[RunDeps] = FunctionToolset()

    @toolset.tool_plain(metadata={"sensitivity": "read"})
    def step(n: int) -> str:
        if fail:
            raise RuntimeError("broken")
        return f"step {n} done"

    return toolset


def _returns(messages: list[ModelMessage]) -> int:
    return sum(
        1
        for message in messages
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, ToolReturnPart)
    )


def _works_for(calls: int) -> FunctionModel:
    """Calls ``work_step`` once per request until it has made ``calls`` calls, then answers."""

    async def stream_fn(
        messages: list[ModelMessage], _info: AgentInfo
    ) -> AsyncIterator[str | DeltaToolCalls]:
        made = _returns(messages)
        if made >= calls:
            yield "all done"
        else:
            yield {0: DeltaToolCall(name="work_step", json_args=json.dumps({"n": made}))}

    return FunctionModel(stream_function=stream_fn)


def _settings(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> None:
    monkeypatch.setattr(engine, "get_settings", lambda: settings)


async def _run(model: FunctionModel, toolset: FunctionToolset[RunDeps]):
    orchestrator = build_chat_orchestrator("do the work", model=model, categories={"work": toolset})
    run = RunRegistry().submit(kind="chat", owner_id="operator", orchestrator=orchestrator)
    await run.wait()
    return run


async def test_a_turn_making_progress_carries_on_past_the_request_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _settings(monkeypatch, Settings(agent_request_limit=3, auto_continue_max=3))
    run = await _run(_works_for(6), _work_toolset())
    assert run.status is RunStatus.done
    notices = [n for n in bodies_of(run, LimitNotice) if n.limit == "continue"]
    assert len(notices) == 2
    assert "1 of 3" in notices[0].message


async def test_the_extensions_run_out(monkeypatch: pytest.MonkeyPatch) -> None:
    _settings(monkeypatch, Settings(agent_request_limit=2, auto_continue_max=1))
    run = await _run(_works_for(10), _work_toolset())
    assert run.status is RunStatus.blocked
    assert [n.limit for n in bodies_of(run, LimitNotice)] == ["continue", "steps"]


async def test_a_turn_whose_calls_all_fail_is_not_extended(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _settings(
        monkeypatch,
        Settings(agent_request_limit=2, auto_continue_max=3, loop_repeat_threshold=99),
    )
    run = await _run(_works_for(10), _work_toolset(fail=True))
    assert run.status is RunStatus.blocked
    assert [n.limit for n in bodies_of(run, LimitNotice)] == ["steps"]


def _fails_first(status: int) -> FunctionModel:
    attempts = 0

    async def stream_fn(
        _messages: list[ModelMessage], _info: AgentInfo
    ) -> AsyncIterator[str | DeltaToolCalls]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ModelHTTPError(status_code=status, model_name="m", body="overloaded")
        yield "answered"

    return FunctionModel(stream_function=stream_fn)


async def test_an_overloaded_server_is_asked_again(monkeypatch: pytest.MonkeyPatch) -> None:
    _settings(monkeypatch, Settings(auto_continue_transient_max=2))
    monkeypatch.setattr(turn, "TRANSIENT_BACKOFF_S", (0.0,))
    run = await _run(_fails_first(503), _work_toolset())
    assert run.status is RunStatus.done
    assert [n.limit for n in bodies_of(run, LimitNotice)] == ["continue"]


async def test_a_refused_request_is_not_sent_again(monkeypatch: pytest.MonkeyPatch) -> None:
    _settings(monkeypatch, Settings(auto_continue_transient_max=2))
    monkeypatch.setattr(turn, "TRANSIENT_BACKOFF_S", (0.0,))
    run = await _run(_fails_first(401), _work_toolset())
    assert run.status is RunStatus.error
    assert not bodies_of(run, LimitNotice)
