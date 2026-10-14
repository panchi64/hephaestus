"""The meta-loop: the no-progress loop-breaker and the optional verifier."""

from __future__ import annotations

from typing import override

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel

import agent.engine as engine
from agent.meta import LoopBreaker, LoopDetected, Verdict, make_utility_judge
from core.config import Settings
from runs import RunRegistry, RunStatus


# --- LoopBreaker (unit) ------------------------------------------------------
def _ask(
    breaker: LoopBreaker, name: str, args: dict[str, object], answer: object, call_id: str
) -> None:
    """One whole tool call: the guard's pre-check, then the answer it came back with."""
    breaker.check(name, args, call_id)
    breaker.observe(call_id, answer)


def test_loop_breaker_trips_on_identical_repeats():
    breaker = LoopBreaker(repeat_threshold=3)
    _ask(breaker, "search", {"q": "x"}, "nothing found", "c1")
    _ask(breaker, "search", {"q": "x"}, "nothing found", "c2")
    with pytest.raises(LoopDetected):
        breaker.check("search", {"q": "x"}, "c3")


def test_loop_breaker_ignores_varied_calls():
    breaker = LoopBreaker(repeat_threshold=2)
    _ask(breaker, "search", {"q": "a"}, "one", "c1")
    _ask(breaker, "search", {"q": "b"}, "one", "c2")  # different args → no trip
    _ask(breaker, "other", {"q": "a"}, "one", "c3")  # different tool → no trip


def test_an_identical_call_that_answers_differently_is_progress():
    # The browser is why this matters: `snapshot()` takes no arguments, so reading the
    # page after the first click, the second and the third is three identical calls — and
    # under an argument-only guard the third one killed the turn, in the middle of exactly
    # the step-by-step work those tools exist for.
    breaker = LoopBreaker(repeat_threshold=3)
    for i, page in enumerate(("home", "search results", "the article", "the comments")):
        _ask(breaker, "snapshot", {}, page, f"c{i}")
    # ...and the guard has not merely been disabled: it still trips once the page stops
    # moving, which is the thing it was always trying to catch.
    _ask(breaker, "snapshot", {}, "the comments", "c9")
    with pytest.raises(LoopDetected):
        breaker.check("snapshot", {}, "c10")


def test_a_call_that_answers_the_same_after_moving_starts_its_count_over():
    # The count is of *consecutive* identical answers. A page that returns to a state it
    # was in before is not evidence of a loop — the model got somewhere and came back.
    breaker = LoopBreaker(repeat_threshold=3)
    _ask(breaker, "snapshot", {}, "list", "c1")
    _ask(breaker, "snapshot", {}, "detail", "c2")
    _ask(breaker, "snapshot", {}, "list", "c3")
    breaker.check("snapshot", {}, "c4")  # would have tripped on a running total


def test_the_same_failure_three_times_is_a_loop():
    # A failure is an answer like any other, and repeating one is the commonest loop
    # there is: the model retries the identical call and reads the identical error.
    breaker = LoopBreaker(repeat_threshold=3)
    _ask(breaker, "click", {"selector": "#go"}, "element not found", "c1")
    _ask(breaker, "click", {"selector": "#go"}, "element not found", "c2")
    with pytest.raises(LoopDetected):
        breaker.check("click", {"selector": "#go"}, "c3")


def test_an_unsettled_call_never_advances_the_count():
    # A call whose result never arrives (an abandoned turn) leaves a pending entry and
    # nothing else — it must not be counted as an answer that failed to change.
    breaker = LoopBreaker(repeat_threshold=2)
    breaker.check("search", {"q": "x"}, "c1")
    breaker.check("search", {"q": "x"}, "c2")
    breaker.check("search", {"q": "x"}, "c3")


def test_an_unserializable_answer_is_still_compared():
    # Fingerprinting falls back to `repr`, so a result carrying an object json cannot
    # render is compared like anything else rather than crashing the guard.
    class Opaque:
        @override
        def __repr__(self) -> str:
            return "<same>"

    breaker = LoopBreaker(repeat_threshold=3)
    _ask(breaker, "grab", {}, Opaque(), "c1")
    _ask(breaker, "grab", {}, Opaque(), "c2")
    with pytest.raises(LoopDetected):
        breaker.check("grab", {}, "c3")


# --- Loop-breaker wired into a run -------------------------------------------
async def test_run_blocks_when_loop_detected(monkeypatch: pytest.MonkeyPatch):
    # threshold=1 trips on the first tool call — exercises the wiring end to end.
    monkeypatch.setattr(engine, "get_settings", lambda: Settings(loop_repeat_threshold=1))
    reg = RunRegistry()
    orch = engine.build_chat_orchestrator("use a tool", model=TestModel(custom_output_text="x"))
    run = reg.submit(kind="chat", owner_id="operator", orchestrator=orch)
    await run.wait()

    assert run.status is RunStatus.blocked
    types = [e.body.type for e in run.stream.replay()]
    assert "limit.notice" in types
    loop_notice = next(e.body for e in run.stream.replay() if e.body.type == "limit.notice")
    assert loop_notice.limit == "loop"


# --- Verifier ----------------------------------------------------------------
async def test_utility_judge_forwards_reasoning_off_settings():
    # The judge is background work that needn't reason: its model_settings (the
    # utility model's reasoning-off settings) must reach the model call.
    captured: dict[str, dict[str, object]] = {}

    async def capture(_messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        captured["settings"] = dict(info.model_settings or {})
        tool = info.output_tools[0].name
        return ModelResponse(parts=[ToolCallPart(tool_name=tool, args={"ok": True})])

    judge = make_utility_judge(
        FunctionModel(capture),
        model_settings={"extra_body": {"chat_template_kwargs": {"enable_thinking": False}}},
    )
    verdict = await judge("the request", "the answer")
    assert verdict.ok is True
    assert captured["settings"]["extra_body"] == {
        "chat_template_kwargs": {"enable_thinking": False}
    }


async def test_verifier_disabled_by_default():
    calls: list[tuple[str, str]] = []

    async def judge(request: str, answer: str) -> Verdict:
        calls.append((request, answer))
        return Verdict(ok=False, reason="should not run")

    reg = RunRegistry()
    orch = engine.build_chat_orchestrator(
        "hello", model=TestModel(custom_output_text="hi"), categories={}, judge=judge
    )
    run = reg.submit(kind="chat", owner_id="operator", orchestrator=orch)
    await run.wait()

    assert run.status is RunStatus.done
    assert calls == []  # verify_enabled is False by default


async def test_verifier_makes_one_corrective_attempt(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        engine, "get_settings", lambda: Settings(verify_enabled=True, verify_heuristic=False)
    )
    verdicts = [Verdict(ok=False, reason="missing the summary")]
    seen: list[str] = []

    async def judge(_request: str, answer: str) -> Verdict:
        seen.append(answer)
        return verdicts.pop(0) if verdicts else Verdict(ok=True)

    reg = RunRegistry()
    orch = engine.build_chat_orchestrator(
        "summarize it", model=TestModel(custom_output_text="here"), categories={}, judge=judge
    )
    run = reg.submit(kind="chat", owner_id="operator", orchestrator=orch)
    await run.wait()

    assert run.status is RunStatus.done
    assert len(seen) == 1  # judged once; one bounded re-attempt, no re-judge
    types = [e.body.type for e in run.stream.replay()]
    assert "limit.notice" in types
    notice = next(e.body for e in run.stream.replay() if e.body.type == "limit.notice")
    assert notice.limit == "verify"


async def test_verifier_accepts_a_good_answer(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        engine, "get_settings", lambda: Settings(verify_enabled=True, verify_heuristic=False)
    )

    async def judge(_request: str, _answer: str) -> Verdict:
        return Verdict(ok=True)

    reg = RunRegistry()
    orch = engine.build_chat_orchestrator(
        "hello", model=TestModel(custom_output_text="hi"), categories={}, judge=judge
    )
    run = reg.submit(kind="chat", owner_id="operator", orchestrator=orch)
    await run.wait()

    assert run.status is RunStatus.done
    types = [e.body.type for e in run.stream.replay()]
    assert "limit.notice" not in types  # no re-attempt


async def test_verifier_heuristic_skips_toolless_turn(monkeypatch: pytest.MonkeyPatch):
    # Default heuristic on: a chitchat turn that called no tools isn't judged.
    monkeypatch.setattr(engine, "get_settings", lambda: Settings(verify_enabled=True))
    judged: list[str] = []

    async def judge(_request: str, answer: str) -> Verdict:
        judged.append(answer)
        return Verdict(ok=False, reason="should not run")

    reg = RunRegistry()
    orch = engine.build_chat_orchestrator(
        "hi", model=TestModel(custom_output_text="hello"), categories={}, judge=judge
    )
    run = reg.submit(kind="chat", owner_id="operator", orchestrator=orch)
    await run.wait()

    assert run.status is RunStatus.done
    assert judged == []  # no checkable artifact (no tool call) → judge skipped


async def test_verifier_heuristic_off_judges_every_turn(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        engine, "get_settings", lambda: Settings(verify_enabled=True, verify_heuristic=False)
    )
    judged: list[str] = []

    async def judge(_request: str, answer: str) -> Verdict:
        judged.append(answer)
        return Verdict(ok=True)

    reg = RunRegistry()
    orch = engine.build_chat_orchestrator(
        "hi", model=TestModel(custom_output_text="hello"), categories={}, judge=judge
    )
    run = reg.submit(kind="chat", owner_id="operator", orchestrator=orch)
    await run.wait()

    assert run.status is RunStatus.done
    assert judged == ["hello"]  # judged even with no tools when the heuristic is off
