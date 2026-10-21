"""A guarded call asks at every level, and nothing but the operator answers it.

Two enforcement points: the deferred path (`settle_deferred`) for calls the level gates,
and `GuardedToolset` for calls nothing defers, which at Yolo is every call. The nested
`run_code` case rides the second and then the first.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from typing import Literal

import pytest
from pydantic_ai import (
    Agent,
    DeferredToolRequests,
    FunctionToolset,
    RunContext,
    ToolApproved,
    ToolCallPart,
    ToolDenied,
)
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel
from pydantic_ai.tools import DeferredToolResults

import agent.gating as gating
from agent import stream_agent_run
from agent.factory import build_agent
from agent.history import TurnStart
from agent.parking import ParkedTurn, announcements, park_for_input
from agent.turn import drive_turn
from core.config import get_settings
from core.container import ServiceContainer
from core.db import init_db, make_engine
from runs import ApprovalRequired, Run, RunStatus, RunStream, ToolFailed
from services.approval_grants import ApprovalGrantStore
from services.conversations import ConversationBinding
from services.permissions import PermissionLevel, guards
from services.permissions.guards import GUARD_KEY
from services.sandbox.fence import ConfinementState
from tools import RunDeps, build_agent_toolsets
from tools.code_mode_marks import RUN_CODE

from ._helpers import run_bodies

type FenceState = Literal["active", "disabled_by_setting", "unavailable"]

OWNER = "operator"
CONV = "conv-1"
DANGEROUS = "rm -rf ~"


def _call(command: str, call_id: str = "c1", *, reach: str = "offline") -> ToolCallPart:
    return ToolCallPart(
        tool_name="shell_run_command",
        args={"command": command, "reach": reach},
        tool_call_id=call_id,
    )


def _shell_category(ran: list[str]) -> dict[str, FunctionToolset[RunDeps]]:
    toolset: FunctionToolset[RunDeps] = FunctionToolset()

    @toolset.tool
    async def run_command(ctx: RunContext[RunDeps], command: str, reach: str = "offline") -> str:
        _ = ctx, reach
        ran.append(command)
        return "ran"

    return {"shell": toolset}


async def _settle(
    permission: PermissionLevel,
    calls: list[ToolCallPart],
    *,
    caps: ServiceContainer | None = None,
    run: Run | None = None,
):
    run = run or Run(id="r1", kind="chat", owner_id=OWNER, stream=RunStream())
    return await gating.settle_deferred(
        run,
        calls,
        caps=caps if caps is not None else ServiceContainer(),
        conversation_id=CONV,
        deps=RunDeps(run=run, owner_id=OWNER, permission=permission),
        messages=[],
        permission=permission,
    )


class TestTheDeferredPath:
    @pytest.mark.parametrize("level", ["manual", "edit", "auto", "yolo"])
    async def test_a_guarded_call_goes_to_the_operator_at_every_level(
        self, level: PermissionLevel, monkeypatch: pytest.MonkeyPatch
    ):
        asked: list[str] = []

        async def spy(
            _run: Run, calls: Sequence[ToolCallPart], **_kwargs: object
        ) -> dict[str, object]:
            asked.extend(call.tool_call_id for call in calls)
            return {}

        monkeypatch.setattr(gating, "review_batch", spy)
        settled, manual = await _settle(level, [_call(DANGEROUS)])
        assert asked == []
        assert [c.tool_call_id for c in manual] == ["c1"]
        assert settled == {}

    async def test_an_ordinary_command_is_not_held_at_yolo(self):
        settled, manual = await _settle("yolo", [_call("ls")])
        assert manual == []
        assert isinstance(settled["c1"], ToolApproved)

    @pytest.mark.parametrize("decisive", [False, True])
    async def test_a_standing_grant_does_not_answer_it(self, decisive: bool):
        engine = make_engine("sqlite:///:memory:")
        init_db(engine)
        grants = ApprovalGrantStore(engine, 3600)
        _ = await grants.grant(OWNER, CONV, "shell_run_command", decisive=decisive)
        caps = ServiceContainer.of(grants)
        # The grant does cover an ordinary command at this level...
        settled, manual = await _settle("edit", [_call("ls", "ok")], caps=caps)
        assert manual == [] and "ok" in settled
        # ...and does not cover the guarded one.
        settled, manual = await _settle("edit", [_call(DANGEROUS)], caps=caps)
        assert [c.tool_call_id for c in manual] == ["c1"]

    async def test_a_level_that_refuses_outright_still_refuses(self):
        settled, manual = await _settle("plan", [_call(DANGEROUS)])
        assert manual == []
        assert isinstance(settled["c1"], ToolDenied)

    async def test_the_host_fence_being_down_holds_a_host_command(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(
            guards, "confinement_state", lambda _s: ConfinementState("unavailable", "no bwrap")
        )
        _, manual = await _settle("yolo", [_call("make install", reach="host")])
        assert [c.tool_call_id for c in manual] == ["c1"]
        _, manual = await _settle("yolo", [_call("make install", reach="offline")])
        assert manual == []


class TestTheCard:
    async def test_the_park_announces_the_reason_and_no_grant_choice(self):
        run = Run(id="r1", kind="chat", owner_id=OWNER, stream=RunStream())
        _, manual = await _settle("manual", [_call(DANGEROUS)], run=run)
        requests = DeferredToolRequests(approvals=manual)
        await park_for_input(
            run, Agent(FunctionModel(_answer), deps_type=object), [], requests, set()
        )

        assert run.status is RunStatus.awaiting_input
        [event] = [b for b in run_bodies(run) if isinstance(b, ApprovalRequired)]
        assert event.guard is not None
        assert "home folder" in event.guard
        assert event.explanation == event.guard
        assert guards.is_guarded(requests.metadata["c1"])
        payload = run.parked_payload
        assert isinstance(payload, ParkedTurn)

    def test_a_restored_park_reads_the_reason_off_its_metadata(self):
        requests = DeferredToolRequests(
            approvals=[_call(DANGEROUS)], metadata={"c1": {GUARD_KEY: "deletes your home"}}
        )
        events, _, _ = announcements(requests, {})
        [event] = events
        assert isinstance(event, ApprovalRequired)
        assert event.guard == "deletes your home"

    def test_an_ordinary_call_carries_no_guard(self):
        events, _, _ = announcements(DeferredToolRequests(approvals=[_call("ls")]), {})
        [event] = events
        assert isinstance(event, ApprovalRequired)
        assert event.guard is None

    @pytest.mark.parametrize("state", ["unavailable", "disabled_by_setting"])
    def test_a_host_command_without_a_fence_carries_the_warning(
        self, state: FenceState, monkeypatch: pytest.MonkeyPatch
    ):
        reason = "why the fence is down"
        monkeypatch.setattr(guards, "confinement_state", lambda _s: ConfinementState(state, reason))
        host = ToolCallPart(
            tool_name="code_run_host_command",
            args={"command": "ls", "explanation": "look"},
            tool_call_id="h1",
        )
        events, _, _ = announcements(
            DeferredToolRequests(approvals=[host, _call("make", "h2", reach="host")]), {}
        )
        assert [e.unconfined for e in events if isinstance(e, ApprovalRequired)] == [reason, reason]

    def test_a_vm_command_carries_none(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(
            guards, "confinement_state", lambda _s: ConfinementState("unavailable", "no bwrap")
        )
        events, _, _ = announcements(DeferredToolRequests(approvals=[_call("make")]), {})
        [event] = events
        assert isinstance(event, ApprovalRequired)
        assert event.unconfined is None


async def _answer(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
    return ModelResponse(parts=[TextPart("done")])


class TestTheYoloPath:
    async def _turn(
        self, command: str, ran: list[str]
    ) -> tuple[Run, Agent[RunDeps, str | DeferredToolRequests]]:
        agent = Agent(
            _scripted("", direct=("shell_run_command", {"command": command})),
            output_type=[str, DeferredToolRequests],
            toolsets=build_agent_toolsets(_shell_category(ran)),
            deps_type=RunDeps,
        )
        run = Run(id="r1", kind="chat", owner_id=OWNER, stream=RunStream())
        _ = await drive_turn(
            run,
            agent,
            settings=get_settings(),
            prompt="go",
            announced=set(),
            binding=ConversationBinding(permission="yolo"),
            turn_start=TurnStart(0),
        )
        return run, agent

    async def test_it_parks_with_the_guard_reason_and_runs_nothing(self):
        ran: list[str] = []
        run, _ = await self._turn(DANGEROUS, ran)

        assert ran == []
        assert run.status is RunStatus.awaiting_input
        [event] = [b for b in run_bodies(run) if isinstance(b, ApprovalRequired)]
        assert event.guard is not None
        assert "home folder" in event.guard

    async def test_after_approval_it_runs_once(self):
        ran: list[str] = []
        run, agent = await self._turn(DANGEROUS, ran)
        parked = run.parked_payload
        assert isinstance(parked, ParkedTurn)

        resumed = Run(id="r2", kind="chat", owner_id=OWNER, stream=RunStream())
        result = await drive_turn(
            resumed,
            agent,
            settings=get_settings(),
            prompt=None,
            message_history=parked.message_history,
            deferred_results=DeferredToolResults(
                approvals={call.tool_call_id: ToolApproved() for call in parked.requests.approvals}
            ),
            announced=set(),
            binding=ConversationBinding(permission="yolo"),
            turn_start=TurnStart(0),
        )
        assert ran == [DANGEROUS]
        assert result.answer == "done"

    async def test_an_ordinary_command_runs_without_asking(self):
        ran: list[str] = []
        run, _ = await self._turn("ls", ran)
        assert ran == ["ls"]
        assert run.status is not RunStatus.awaiting_input


def _scripted(code: str, *, direct: tuple[str, dict[str, str]] | None = None) -> FunctionModel:
    """A model that sends one call, a ``run_code`` script or ``direct``, then answers."""
    sent: list[int] = []

    async def stream_fn(
        _messages: list[ModelMessage], _info: AgentInfo
    ) -> AsyncIterator[str | dict[int, DeltaToolCall]]:
        sent.append(1)
        if len(sent) > 1:
            yield "done"
        elif direct is not None:
            yield {0: DeltaToolCall(name=direct[0], json_args=json.dumps(direct[1]))}
        else:
            yield {0: DeltaToolCall(name=RUN_CODE, json_args=json.dumps({"code": code}))}

    return FunctionModel(stream_function=stream_fn)


async def _run_scripted(
    model: FunctionModel, ran: list[str], permission: PermissionLevel
) -> tuple[Run, DeferredToolRequests | str]:
    agent = build_agent(model, categories=_shell_category(ran))
    run = Run(id="t", kind="chat", owner_id=OWNER, stream=RunStream())
    deps = RunDeps(run=run, owner_id=OWNER, permission=permission)
    async with agent.iter("go", deps=deps) as agent_run:
        await stream_agent_run(agent_run, run)
    assert agent_run.result is not None
    return run, agent_run.result.output


class TestANestedCall:
    CODE = (
        'out = ""\n'
        "try:\n"
        '    await shell_run_command(command="rm -rf ~")\n'
        "except Exception as e:\n"
        "    out = str(e)\n"
        'await shell_run_command(command="ls")\n'
        "out"
    )

    async def test_a_script_cannot_run_a_guarded_call(self):
        ran: list[str] = []
        run, output = await _run_scripted(_scripted(self.CODE), ran, "yolo")

        assert ran == ["ls"]
        assert not isinstance(output, DeferredToolRequests)
        failures = [b for b in run_bodies(run) if isinstance(b, ToolFailed)]
        assert any("directly, outside `run_code`" in f.error for f in failures)

    async def test_the_same_call_made_directly_parks_at_yolo(self):
        ran: list[str] = []
        model = _scripted("", direct=("shell_run_command", {"command": DANGEROUS}))
        _, output = await _run_scripted(model, ran, "yolo")

        assert isinstance(output, DeferredToolRequests)
        [call] = output.approvals
        assert call.tool_name == "shell_run_command"
        assert guards.is_guarded(output.metadata[call.tool_call_id])
        assert ran == []
