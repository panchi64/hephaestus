"""Reading a long prompt is silent, and that silence is not a stalled run.

The inactivity watchdog stops a run that emits nothing for its bound. A local model reading a
cold, long prompt emits nothing for as long as the read takes, so the wait for each request's
first part holds the clock open, and the first-token bound is what stops a server that never
answers.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models.function import AgentInfo, FunctionModel

import agent.engine as engine
from agent import build_chat_orchestrator
from core.config import Settings
from runs import LimitNotice, RunRegistry, RunStatus

from ._helpers import bodies_of


def _answers_after(delay_s: float) -> FunctionModel:
    async def stream_fn(_messages: list[ModelMessage], _info: AgentInfo) -> AsyncIterator[str]:
        await asyncio.sleep(delay_s)
        yield "read it all"

    return FunctionModel(stream_function=stream_fn)


def _bound(monkeypatch: pytest.MonkeyPatch, first_token_timeout_s: float | None) -> None:
    monkeypatch.setattr(
        engine, "get_settings", lambda: Settings(first_token_timeout_s=first_token_timeout_s)
    )


async def test_a_slow_first_token_outlasts_the_inactivity_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _bound(monkeypatch, 30.0)
    registry = RunRegistry(inactivity_timeout_s=0.2)
    orchestrator = build_chat_orchestrator("summarize", model=_answers_after(0.6), categories={})
    run = registry.submit(kind="chat", owner_id="operator", orchestrator=orchestrator)
    await run.wait()
    assert run.status is RunStatus.done


async def test_without_the_bound_the_same_wait_is_stopped_as_idle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _bound(monkeypatch, None)
    registry = RunRegistry(inactivity_timeout_s=0.2)
    orchestrator = build_chat_orchestrator("summarize", model=_answers_after(0.6), categories={})
    run = registry.submit(kind="chat", owner_id="operator", orchestrator=orchestrator)
    await run.wait()
    assert run.status is RunStatus.blocked


async def test_a_server_that_never_answers_is_stopped_at_the_first_token_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _bound(monkeypatch, 0.2)
    registry = RunRegistry(inactivity_timeout_s=None)
    orchestrator = build_chat_orchestrator("summarize", model=_answers_after(30.0), categories={})
    run = registry.submit(kind="chat", owner_id="operator", orchestrator=orchestrator)
    await run.wait()
    assert run.status is RunStatus.blocked
    [notice] = [n for n in bodies_of(run, LimitNotice) if n.limit == "time"]
    assert "the model sent nothing" in notice.message
