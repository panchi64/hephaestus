"""Who an agent may message unasked: anyone up its tree, and read-only agents anywhere in it.
A message that reaches an agent able to write from across or below asks first."""

from __future__ import annotations

import pytest
from pydantic_ai import RunContext
from pydantic_ai.exceptions import ApprovalRequired
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage

from core.container import ServiceContainer
from runs import Run, RunStream
from services.permissions import PERMISSION_LEVELS, beyond_scope
from services.permissions.decide import Decision, decide
from services.subagents import Recipient, SubagentLauncher
from tools.agents import agents_toolset
from tools.deps import RunDeps

OWNER = "operator"

RECIPIENTS = {
    "parent": Recipient("conv-parent", "main", None, upward=True),
    "lead": Recipient("conv-lead", "lead", "worker", upward=True),
    "reader": Recipient("conv-reader", "reader", "explorer", upward=False),
    "fixer": Recipient("conv-fixer", "fixer", "worker", upward=False),
    "gone": Recipient("conv-gone", "gone", "no-such-agent", upward=False),
}


class FakeLauncher:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def recipient(self, owner_id: str, conversation_id: str, to: str) -> Recipient:
        return RECIPIENTS[to]

    async def message(self, owner_id: str, conversation_id: str, to: str, text: str) -> str:
        self.sent.append((to, text))
        return RECIPIENTS[to].label


def _ctx(launcher: FakeLauncher, *, approved: bool = False) -> RunContext[RunDeps]:
    caps = ServiceContainer()
    caps.add(launcher, as_type=SubagentLauncher)
    run = Run(id="run-messaging", kind="linked", owner_id=OWNER, stream=RunStream())
    ctx = RunContext(
        deps=RunDeps(run=run, owner_id=OWNER, caps=caps, conversation_id="conv-me"),
        model=TestModel(),
        usage=RunUsage(),
    )
    ctx.tool_call_approved = approved
    return ctx


async def _message(ctx: RunContext[RunDeps], to: str) -> dict:
    toolset = agents_toolset()
    tools = await toolset.get_tools(ctx)
    return await toolset.call_tool("message", {"to": to, "text": "found it"}, ctx, tools["message"])


@pytest.mark.parametrize("to", ["parent", "lead", "reader"])
async def test_up_the_tree_or_to_a_read_only_agent_goes_unasked(to):
    launcher = FakeLauncher()
    result = await _message(_ctx(launcher), to)
    assert result == {"sent": True, "to": RECIPIENTS[to].label}
    assert launcher.sent == [(to, "found it")]


@pytest.mark.parametrize("to", ["fixer", "gone"])
async def test_across_or_down_to_an_agent_that_can_write_asks_first(to):
    launcher = FakeLauncher()
    with pytest.raises(ApprovalRequired):
        await _message(_ctx(launcher), to)
    assert launcher.sent == []


async def test_an_approved_message_to_a_writer_is_sent():
    launcher = FakeLauncher()
    result = await _message(_ctx(launcher, approved=True), "fixer")
    assert result["sent"] is True


def test_a_read_only_agent_is_offered_the_tool_at_every_level():
    for level in PERMISSION_LEVELS:
        assert not beyond_scope(level, "agents_message")


def test_its_own_gate_is_answered_by_the_operator_or_the_review():
    assert decide("plan", "agents_message") is Decision.ASK
    assert decide("auto", "agents_message") is Decision.REVIEW
