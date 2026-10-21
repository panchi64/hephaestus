"""Approval flow: a sensitive tool parks the run; approve/deny resumes it."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

from pydantic_ai import (
    FunctionToolset,
    ModelMessage,
    RunContext,
    ToolApproved,
    ToolDenied,
    ToolReturnPart,
)
from pydantic_ai.exceptions import ApprovalRequired as AskFirst
from pydantic_ai.models.function import AgentInfo
from pydantic_ai.models.test import TestModel

from agent import ParkedTurn, build_chat_orchestrator, build_resume_orchestrator
from core.container import ServiceContainer
from core.db import init_db, make_engine
from core.vault import Vault
from runs import ApprovalRequired, Run, RunRegistry, RunStatus, ToolCompleted
from services.conversations import ConversationBinding
from services.notifications import NotificationService
from services.tool_sensitivity import SENSITIVITY_METADATA_KEY, Sensitivity
from tools import RunDeps

from ._helpers import bodies_of, parked_turn, run_bodies
from ._json import dump


def _danger_categories():
    toolset: FunctionToolset[RunDeps] = FunctionToolset()

    @toolset.tool_plain(requires_approval=True)
    def delete_thing(name: str) -> str:
        return f"deleted {name}"

    return {"danger": toolset}


def _types(run: Run) -> list[str]:
    return [b.type for b in run_bodies(run)]


async def _park_a_run(reg: RunRegistry):
    orch = build_chat_orchestrator(
        "delete the thing",
        model=TestModel(custom_output_text="done"),
        categories=_danger_categories(),
    )
    run = reg.submit(kind="chat", owner_id="operator", orchestrator=orch)
    await run.wait()
    return run


async def test_sensitive_tool_parks_for_approval():
    reg = RunRegistry()
    run = await _park_a_run(reg)

    assert run.status is RunStatus.awaiting_input
    assert not run.stream.closed  # stream stays open for the resume
    types = _types(run)
    assert "approval.required" in types
    assert "tool.completed" not in types  # not executed — only requested
    assert "run.ended" not in types  # not terminal

    approval = next(e.body for e in run.stream.replay() if e.body.type == "approval.required")
    assert "delete_thing" in approval.name
    assert "name" in approval.args
    assert approval.summary.startswith(approval.name)
    assert isinstance(run.parked_payload, ParkedTurn)


async def test_approved_resume_executes_and_completes():
    reg = RunRegistry()
    run = await _park_a_run(reg)
    parked = parked_turn(run)
    call_id = parked.requests.approvals[0].tool_call_id

    resumed = await reg.resume(run.id, build_resume_orchestrator(parked, {call_id: ToolApproved()}))
    assert resumed is run
    await run.wait()

    assert run.status is RunStatus.done
    types = _types(run)
    assert "tool.completed" in types  # executed after approval
    assert types[-1] == "run.ended"
    assert run.stream.closed
    # tool.started was announced once (defer turn), not duplicated on resume
    assert _types(run).count("tool.started") == 1


async def test_denied_resume_completes_without_executing():
    reg = RunRegistry()
    run = await _park_a_run(reg)
    parked = parked_turn(run)
    call_id = parked.requests.approvals[0].tool_call_id

    _ = await reg.resume(
        run.id, build_resume_orchestrator(parked, {call_id: ToolDenied(message="no")})
    )
    await run.wait()

    assert run.status is RunStatus.done
    # The denial is surfaced to the model as the call's result, but the tool
    # body never ran — no real side effect.
    completed = bodies_of(run, ToolCompleted)
    assert all("deleted" not in str(dump(b)["result"]) for b in completed)


async def test_cancel_parked_run():
    reg = RunRegistry()
    run = await _park_a_run(reg)

    assert await reg.cancel(run.id) is True
    assert run.status is RunStatus.cancelled
    assert run.stream.closed
    assert _types(run)[-1] == "run.ended"


# --- approval_needed notification wiring --------------------------------------------


async def _notification_service(tmp_path: Path) -> NotificationService:
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    vault = Vault(tmp_path / "keyfile.json")
    await vault.setup("pw")
    service = NotificationService(engine, vault)
    await service.start()
    return service


async def test_park_notifies_approval_needed(tmp_path: Path):
    service = await _notification_service(tmp_path)
    reg = RunRegistry()
    orch = build_chat_orchestrator(
        "delete the thing",
        model=TestModel(custom_output_text="done"),
        categories=_danger_categories(),
        capabilities=ServiceContainer.of(service),
        conversation_id="c1",
    )
    run = reg.submit(kind="chat", owner_id="operator", orchestrator=orch, conversation_id="c1")
    await run.wait()

    assert run.status is RunStatus.awaiting_input
    items, unread = await service.list_notifications("operator")
    assert unread == 1
    assert items[0].kind == "approval_needed"
    assert items[0].run_id == run.id
    assert items[0].conversation_id == "c1"
    assert "delete_thing" in items[0].title
    await service.stop()


async def test_a_self_gated_tools_metadata_explanation_reaches_the_card():
    toolset: FunctionToolset[RunDeps] = FunctionToolset()

    @toolset.tool(metadata={SENSITIVITY_METADATA_KEY: Sensitivity.READ.value})
    def forget(ctx: RunContext[RunDeps], name: str) -> str:
        if not ctx.tool_call_approved:
            raise AskFirst(metadata={"explanation": f"Forget “{name}”"})
        return "gone"

    reg = RunRegistry()
    orch = build_chat_orchestrator(
        "forget it",
        model=TestModel(custom_output_text="done"),
        categories={"danger": toolset},
        binding=ConversationBinding(permission="edit"),
    )
    run = reg.submit(kind="chat", owner_id="operator", orchestrator=orch)
    await run.wait()

    assert run.status is RunStatus.awaiting_input
    (asked,) = bodies_of(run, ApprovalRequired)
    assert asked.explanation is not None and asked.explanation.startswith("Forget “")


async def test_grant_short_circuit_resolves_a_dangling_notification(tmp_path: Path):
    # Everything this turn defers is already grant-covered from the start, so the
    # engine never parks — it continues the same turn inline. That branch is a
    # defensive `resolve_for_run` call too: this proves it actually fires by seeding a
    # notification for this exact run id up front and checking it's resolved after.
    from tests._helpers import granting_store

    # Learn the real (namespaced) tool name a park reports, rather than guessing it.
    probe_run = await _park_a_run(RunRegistry())
    tool_name = parked_turn(probe_run).requests.approvals[0].tool_name

    service = await _notification_service(tmp_path)
    grants = await granting_store("operator", "c1", tool_name)
    run_id = "preset-run-1"
    _ = await service.notify("operator", "approval_needed", "stale", run_id=run_id)

    reg = RunRegistry()
    orch = build_chat_orchestrator(
        "delete the thing",
        model=TestModel(custom_output_text="done"),
        categories=_danger_categories(),
        capabilities=ServiceContainer.of(grants, service),
        conversation_id="c1",
        # Edit rather than the default, which is Auto: there a grant feeds the review
        # instead of settling the call on its own, and with no utility model bound the
        # turn would park — which is the branch under test not firing.
        binding=ConversationBinding(permission="edit"),
    )
    run = reg.submit(
        kind="chat",
        owner_id="operator",
        orchestrator=orch,
        conversation_id="c1",
        run_id=run_id,
    )
    await run.wait()

    assert run.status is RunStatus.done  # grant-covered inline — never parked
    items, _ = await service.list_notifications("operator")
    assert items[0].resolved_at is not None
    await service.stop()


async def test_parked_turn_keeps_parallel_tool_calls_on_resume():
    # Why the setting sits on the agent and not on `agent.iter(...)`: a park stashes
    # this agent on the ParkedTurn, so the resume inherits it with nothing threaded
    # through the payload and nothing to go stale in an older one.
    reg = RunRegistry()
    run = await _park_a_run(reg)
    parked = parked_turn(run)

    assert parked.agent is not None
    settings = parked.agent.model_settings
    assert isinstance(settings, dict)
    assert settings.get("parallel_tool_calls") is True

    call_id = parked.requests.approvals[0].tool_call_id
    resumed = await reg.resume(run.id, build_resume_orchestrator(parked, {call_id: ToolApproved()}))
    assert resumed is not None
    await resumed.wait()

    assert resumed.status is RunStatus.done
    assert "tool.completed" in _types(resumed)


async def test_a_parked_call_does_not_hold_back_the_plain_call_beside_it():
    """One step, two calls: one the level parks, one it clears. The library runs the plain
    call while the gated one is set aside, so the operator watching the work log sees it
    finish *before* being asked about the other — and the park names only the call that is
    actually waiting, and a resume does not run the finished one a second time."""
    from pydantic_ai.models.function import DeltaToolCall, FunctionModel

    ran: list[str] = []
    toolset: FunctionToolset[RunDeps] = FunctionToolset()

    @toolset.tool_plain(requires_approval=True)
    def delete_thing(name: str) -> str:
        ran.append("delete")
        return f"deleted {name}"

    @toolset.tool_plain(metadata={"sensitivity": "read"})
    def look(name: str) -> str:
        ran.append("look")
        return f"saw {name}"

    async def stream_fn(
        messages: list[ModelMessage], _info: AgentInfo
    ) -> AsyncIterator[str | dict[int, DeltaToolCall]]:
        if any(
            isinstance(part, ToolReturnPart) and part.tool_name == "danger_delete_thing"
            for message in messages
            for part in message.parts
        ):
            yield "done"
            return
        yield {
            0: DeltaToolCall(
                name="danger_delete_thing", json_args='{"name": "x"}', tool_call_id="gated"
            ),
            1: DeltaToolCall(name="danger_look", json_args='{"name": "x"}', tool_call_id="plain"),
        }

    reg = RunRegistry()
    run = reg.submit(
        kind="chat",
        owner_id="operator",
        orchestrator=build_chat_orchestrator(
            "go",
            model=FunctionModel(stream_function=stream_fn),
            categories={"danger": toolset},
        ),
    )
    await run.wait()
    assert run.status is RunStatus.awaiting_input

    bodies = [e.body for e in run.stream.replay()]
    completed = next(
        i for i, b in enumerate(bodies) if b.type == "tool.completed" and b.tool_call_id == "plain"
    )
    asked = next(i for i, b in enumerate(bodies) if b.type == "approval.required")
    assert completed < asked
    parked = parked_turn(run)
    assert [c.tool_call_id for c in parked.requests.approvals] == ["gated"]
    assert ran == ["look"]

    resumed = await reg.resume(run.id, build_resume_orchestrator(parked, {"gated": ToolApproved()}))
    assert resumed is not None
    await resumed.wait()
    assert resumed.status is RunStatus.done
    assert ran == ["look", "delete"]


class TestTheOneLineSummary:
    """`summarize_call` renders a call for a human reading a notification, not for a log.

    The full arguments ride the same event in `args`, untouched. What this string is for is
    the operator's phone telling them *which* call is waiting — and an uncapped one meant
    `plan_submit` put an entire markdown plan in that sentence.
    """

    def test_an_ordinary_call_is_readable_in_full(self):
        from agent.parking import summarize_call

        summary = summarize_call("shell_run", {"command": "rm -rf build/", "timeout": 30})

        # The case the cap must not touch. A shell command the operator is being asked to
        # approve is unreadable truncated, and every one of them fits.
        assert summary == "shell_run(command='rm -rf build/', timeout=30)"

    def test_one_enormous_argument_is_elided_rather_than_carried(self):
        from agent.parking import SUMMARY_MAX_CHARS, VALUE_MAX_CHARS, summarize_call

        summary = summarize_call("plan_submit", {"body": "# Plan\n" + "step. " * 2000})

        assert len(summary) <= SUMMARY_MAX_CHARS
        # Elided, not merely cut: a summary that stopped silently reads as a call with a
        # shorter argument than the one actually being approved. And the value is cut
        # *inside* the call rather than the line being chopped after it, so the rendering
        # still reads as a call.
        assert summary.endswith("…)")
        assert summary.startswith("plan_submit(body='# Plan")
        assert len(summary) > VALUE_MAX_CHARS // 2

    def test_a_later_argument_survives_an_earlier_enormous_one(self):
        from agent.parking import summarize_call

        summary = summarize_call("write", {"body": "x" * 5_000, "path": "notes.md"})

        # Why each value is elided before the line is assembled rather than the line being
        # cut at the end: the last argument is as likely as the first to be the one that
        # says what this call does.
        assert "path='notes.md'" in summary

    def test_many_small_arguments_are_capped_by_the_line(self):
        from agent.parking import SUMMARY_MAX_CHARS, summarize_call

        summary = summarize_call("noisy", {f"k{n}": f"v{n}" for n in range(200)})

        # The other shape: nothing here is individually long, and the line is still a
        # notification body.
        assert len(summary) <= SUMMARY_MAX_CHARS
        assert summary.endswith("…")
