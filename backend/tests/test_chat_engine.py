"""The chat orchestrator on the Run substrate: end-to-end with a TestModel."""

from __future__ import annotations

from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel

import agent.engine as engine
from agent import build_chat_orchestrator
from core.config import Settings
from runs import RunEnded, RunRegistry, RunStatus


def _bodies(run):
    return [e.body for e in run.stream.replay()]


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


async def test_metrics_accumulate_across_verifier_correction(monkeypatch):
    # A verifier correction is a second turn; the reported metrics must cover the
    # whole run, not just the corrective turn.
    from agent.meta import Verdict

    monkeypatch.setattr(
        engine, "get_settings", lambda: Settings(verify_enabled=True, verify_heuristic=False)
    )
    verdicts = [Verdict(ok=False, reason="redo")]

    async def judge(request, answer):
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


async def test_usage_limit_blocks_the_turn(monkeypatch):
    # request_limit=0 trips on the first model request → bounded stop.
    monkeypatch.setattr(
        engine, "get_settings", lambda: Settings(agent_request_limit=0, agent_tool_calls_limit=None)
    )
    reg = RunRegistry()
    orch = build_chat_orchestrator("hello", model=TestModel(custom_output_text="never"))
    run = reg.submit(kind="chat", owner_id="operator", orchestrator=orch)
    await run.wait()

    assert run.status is RunStatus.blocked
    types = [b.type for b in _bodies(run)]
    assert "limit.notice" in types
    notice = next(b for b in _bodies(run) if b.type == "limit.notice")
    assert notice.limit == "steps"
    # The operator-facing message is a plain sentence, not pydantic_ai's own raw
    # internal phrasing (e.g. its `{tool_calls=}` repr syntax) — mirrors the
    # legibility treatment `RunTimeout` gets in `runs/registry.py`.
    assert "request_limit" not in notice.message
    assert notice.message.startswith("this run hit its step limit for a single turn")
    last = _bodies(run)[-1]
    assert isinstance(last, RunEnded)
    assert last.outcome == "blocked"


async def test_usage_limit_stop_marker_matches_the_toast(monkeypatch):
    # The persisted stop marker is the *same* legible sentence as the toast. A bare
    # "usage limit reached" reads as a provider rate limit — the wrong thing to send
    # the operator looking for, since this is one of the run's own local budgets.
    monkeypatch.setattr(
        engine, "get_settings", lambda: Settings(agent_request_limit=0, agent_tool_calls_limit=None)
    )
    reg = RunRegistry()
    orch = build_chat_orchestrator("hello", model=TestModel(custom_output_text="never"))
    run = reg.submit(kind="chat", owner_id="operator", orchestrator=orch)
    await run.wait()

    notice = next(b for b in _bodies(run) if b.type == "limit.notice")
    assert run.detail == notice.message
    assert run.detail is not None
    assert "usage limit" not in run.detail


async def test_explicit_request_limit_overrides_the_config_default(monkeypatch):
    # The operator's setting is what bounds the turn — the config default is only the
    # fallback for a caller that resolved none. A default of 25 with an override of 0
    # must stop on the first model request.
    monkeypatch.setattr(
        engine,
        "get_settings",
        lambda: Settings(agent_request_limit=25, agent_tool_calls_limit=None),
    )
    reg = RunRegistry()
    orch = build_chat_orchestrator(
        "hello", model=TestModel(custom_output_text="never"), request_limit=0
    )
    run = reg.submit(kind="chat", owner_id="operator", orchestrator=orch)
    await run.wait()

    assert run.status is RunStatus.blocked
    assert next(b for b in _bodies(run) if b.type == "limit.notice").limit == "steps"


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


def test_usage_limit_kind_distinguishes_the_tripped_bound():
    from pydantic_ai import UsageLimitExceeded

    from agent.model_errors import usage_limit_kind

    steps = UsageLimitExceeded("The next request would exceed the request_limit of 25")
    assert usage_limit_kind(steps) == "steps"

    tool_calls = UsageLimitExceeded(
        "The next tool call(s) would exceed the tool_calls_limit of 0 (tool_calls=1)."
    )
    assert usage_limit_kind(tool_calls) == "tool_calls"

    tokens = UsageLimitExceeded("Exceeded the total_tokens_limit of 100 (total_tokens=150)")
    assert usage_limit_kind(tokens) == "tokens"


def test_usage_limit_message_is_operator_legible_for_every_kind():
    from pydantic_ai import UsageLimitExceeded

    from agent.model_errors import usage_limit_message

    steps = UsageLimitExceeded("The next request would exceed the request_limit of 25")
    assert usage_limit_message(steps).startswith(
        "this run hit its step limit for a single turn and stopped"
    )

    tool_calls = UsageLimitExceeded(
        "The next tool call(s) would exceed the tool_calls_limit of 0 (tool_calls=1)."
    )
    assert "{tool_calls=}" not in usage_limit_message(tool_calls)
    assert (
        usage_limit_message(tool_calls)
        == "this run hit its tool-call limit for a single turn and stopped"
    )

    tokens = UsageLimitExceeded("Exceeded the total_tokens_limit of 100 (total_tokens=150)")
    assert (
        usage_limit_message(tokens) == "this run hit its token budget for a single turn and stopped"
    )

    # Every one of them names a *per-turn* bound. None may read as an account-level
    # provider quota, which is what sent the operator hunting for a rate limit before.
    for exc in (steps, tool_calls, tokens):
        assert "for a single turn" in usage_limit_message(exc)


async def test_parallel_tool_calls_reaches_the_model_request():
    # The default the product runs on: independent calls go out together, so a
    # multi-part question costs one model round-trip instead of one per part. Asserted
    # on what the *model* is handed rather than on the agent object, because the agent's
    # own settings are merged with each capability's before a request is built.
    seen: list[dict[str, object]] = []

    async def capture(messages, info: AgentInfo):
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
