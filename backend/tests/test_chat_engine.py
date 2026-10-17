"""The chat orchestrator on the Run substrate: end-to-end with a TestModel."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from pydantic_ai import ModelMessage
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel

import agent.engine as engine
from agent import build_chat_orchestrator
from core.config import Settings
from runs import RunRegistry, RunStatus

from ._helpers import run_bodies as _bodies


async def test_chat_runs_to_done_with_metrics():
    reg = RunRegistry()
    orch = build_chat_orchestrator(
        "hello", model=TestModel(custom_output_text="hi there", call_tools=[])
    )
    run = reg.submit(kind="chat", owner_id="operator", orchestrator=orch)
    await run.wait()

    assert run.status is RunStatus.done
    types = [b.type for b in _bodies(run)]
    assert types[0] == "run.started"
    assert types[-1] == "run.ended"
    assert "answer.delta" in types

    answer = "".join(b.text for b in _bodies(run) if b.type == "answer.delta")
    assert answer == "hi there"

    metrics = next(b for b in _bodies(run) if b.type == "run.metrics")
    assert metrics.steps >= 1


async def test_metrics_accumulate_across_verifier_correction(monkeypatch: pytest.MonkeyPatch):
    # A verifier correction is a second turn; the reported metrics must cover the
    # whole run, not just the corrective turn.
    from agent.meta import Verdict

    monkeypatch.setattr(
        engine, "get_settings", lambda: Settings(verify_enabled=True, verify_heuristic=False)
    )
    verdicts = [Verdict(ok=False, reason="redo")]

    async def judge(_request: str, _answer: str) -> Verdict:
        return verdicts.pop(0) if verdicts else Verdict(ok=True)

    reg = RunRegistry()
    orch = build_chat_orchestrator(
        "hello", model=TestModel(custom_output_text="hi"), categories={}, judge=judge
    )
    run = reg.submit(kind="chat", owner_id="operator", orchestrator=orch)
    await run.wait()

    assert run.status is RunStatus.done
    # The live gauge emits a metrics frame per step; the *final* frame is the accumulated one.
    metrics = [b for b in _bodies(run) if b.type == "run.metrics"][-1]
    assert metrics.steps >= 2  # original turn + the corrective re-attempt


def test_drop_dangling_tool_calls_trims_unanswered_trailing_call():
    # A blocked-turn transcript can end on an assistant tool call that never got its result;
    # replaying that to a provider is an HTTP 400, so the model's loaded history must be
    # sanitized. A history ending in text (a real answer) is left alone.
    from pydantic_ai import ModelRequest, ModelResponse, TextPart, ToolCallPart
    from pydantic_ai.messages import UserPromptPart

    from agent.history import drop_dangling_tool_calls

    user = ModelRequest(parts=[UserPromptPart(content="do it")])

    # Trailing unanswered tool call → the whole response is dropped.
    dangling = [user, ModelResponse(parts=[ToolCallPart(tool_name="x", args={}, tool_call_id="1")])]
    assert drop_dangling_tool_calls(dangling) == [user]

    # Text + a dangling call → keep the text, drop only the call part.
    mixed = [
        user,
        ModelResponse(
            parts=[
                TextPart(content="partial"),
                ToolCallPart(tool_name="x", args={}, tool_call_id="1"),
            ]
        ),
    ]
    trimmed = drop_dangling_tool_calls(mixed)
    assert len(trimmed) == 2
    assert [type(p).__name__ for p in trimmed[-1].parts] == ["TextPart"]

    # A clean answer is untouched.
    clean = [user, ModelResponse(parts=[TextPart(content="done")])]
    assert drop_dangling_tool_calls(clean) == clean


async def test_parallel_tool_calls_reaches_the_model_request():
    # The default the product runs on: independent calls go out together, so a
    # multi-part question costs one model round-trip instead of one per part. Asserted
    # on what the *model* is handed rather than on the agent object, because the agent's
    # own settings are merged with each capability's before a request is built.
    seen: list[dict[str, object]] = []

    async def capture(_messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str]:
        seen.append(dict(info.model_settings or {}))
        yield "ok"

    reg = RunRegistry()
    orch = build_chat_orchestrator("hello", model=FunctionModel(stream_function=capture))
    run = reg.submit(kind="chat", owner_id="operator", orchestrator=orch)
    await run.wait()

    assert run.status is RunStatus.done
    assert seen and all(s["parallel_tool_calls"] is True for s in seen)


def test_a_toolless_utility_agent_is_left_alone():
    # Only the tool-driving agent declares it. A background call — namer, judge,
    # summarizer — keeps whatever its caller hands `.run()`.
    from agent.meta import make_utility_agent

    assert make_utility_agent(TestModel(), instructions="name it").model_settings is None
