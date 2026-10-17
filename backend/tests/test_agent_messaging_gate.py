"""Messaging and spawning within a delegation tree never ask: a spawned agent runs at its
spawner's level, so what it does is asked about there."""

from __future__ import annotations

import pytest
from pydantic import JsonValue
from pydantic_ai import RunContext
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage

from core.container import ServiceContainer
from runs import Run, RunStream
from services.permissions import PERMISSION_LEVELS, beyond_scope
from services.subagents import Recipient, SubagentLauncher
from tools.agents import agents_toolset
from tools.deps import RunDeps

from ._json import JSON, obj

OWNER = "operator"

RECIPIENTS = {
    "parent": Recipient("conv-parent", "main", None, upward=True),
    "reader": Recipient("conv-reader", "reader", "explorer", upward=False),
    "fixer": Recipient("conv-fixer", "fixer", "worker", upward=False),
}


class FakeLauncher:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def message(self, _owner_id: str, _conversation_id: str, to: str, text: str) -> str:
        self.sent.append((to, text))
        return RECIPIENTS[to].label


def _ctx(launcher: FakeLauncher) -> RunContext[RunDeps]:
    caps = ServiceContainer()
    caps.add(launcher, as_type=SubagentLauncher)
    run = Run(id="run-messaging", kind="linked", owner_id=OWNER, stream=RunStream())
    return RunContext(
        deps=RunDeps(run=run, owner_id=OWNER, caps=caps, conversation_id="conv-me"),
        model=TestModel(),
        usage=RunUsage(),
    )


async def _message(ctx: RunContext[RunDeps], to: str) -> dict[str, JsonValue]:
    toolset = agents_toolset()
    tools = await toolset.get_tools(ctx)
    return obj(
        JSON.validate_python(
            await toolset.call_tool(
                "message", {"to": to, "text": "found it"}, ctx, tools["message"]
            )
        )
    )


@pytest.mark.parametrize("to", ["parent", "reader", "fixer"])
async def test_a_message_anywhere_in_the_tree_goes_unasked(to: str):
    launcher = FakeLauncher()
    result = await _message(_ctx(launcher), to)
    assert result == {"sent": True, "to": RECIPIENTS[to].label}
    assert launcher.sent == [(to, "found it")]


@pytest.mark.parametrize("tool", ["agents_message", "agents_spawn"])
def test_coordinating_is_offered_unasked_at_every_level(tool: str):
    for level in PERMISSION_LEVELS:
        assert not beyond_scope(level, tool)
