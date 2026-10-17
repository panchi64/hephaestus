"""A turn has no request budget, and a request a transient model error cut short is sent
again from the last complete request in the same run."""

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
from runs import AnswerRetracted, LimitNotice, RunRegistry, RunStatus
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


async def test_a_long_turn_runs_past_the_library_default_request_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # pydantic-ai caps a run at 50 requests unless told otherwise.
    _settings(monkeypatch, Settings())
    run = await _run(_works_for(70), _work_toolset())
    assert run.status is RunStatus.done
    assert bodies_of(run, LimitNotice) == []


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


async def test_a_retried_answer_takes_back_what_the_cut_stream_showed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The live bubble otherwise shows the first half and then the whole answer after it.
    _settings(monkeypatch, Settings(auto_continue_transient_max=2))
    monkeypatch.setattr(turn, "TRANSIENT_BACKOFF_S", (0.0,))
    attempts = 0

    async def stream_fn(
        _messages: list[ModelMessage], _info: AgentInfo
    ) -> AsyncIterator[str | DeltaToolCalls]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            yield "half an ans"
            raise ModelHTTPError(status_code=502, model_name="m", body="stream cut")
        yield "the full answer"

    run = await _run(FunctionModel(stream_function=stream_fn), _work_toolset())
    assert run.status is RunStatus.done
    [retracted] = bodies_of(run, AnswerRetracted)
    assert (retracted.text_chars, retracted.thinking_chars) == (len("half an ans"), 0)


async def test_a_refused_request_is_not_sent_again(monkeypatch: pytest.MonkeyPatch) -> None:
    _settings(monkeypatch, Settings(auto_continue_transient_max=2))
    monkeypatch.setattr(turn, "TRANSIENT_BACKOFF_S", (0.0,))
    run = await _run(_fails_first(401), _work_toolset())
    assert run.status is RunStatus.error
    assert not bodies_of(run, LimitNotice)
