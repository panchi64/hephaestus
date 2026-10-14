"""The workspace-kind filter, and the tripwire under its literal tool names.

`services/workspace_kind.py` names each kind's tools as literal strings, because `tools/`
sits above `services/` and importing the catalog there would invert the dependency order.
A literal set rots silently: a fifth shell tool missing from it would be offered to sandbox
chats. So the names are checked against the catalog a real run resolves, both ways.
"""

from __future__ import annotations

from pydantic_ai import RunContext
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage

from runs import Run, RunStream
from services.workspace_kind import (
    WORKSPACE_TOOLS,
    WorkspaceKind,
    workspace_disabled_tools,
    workspace_kind,
)
from tools.catalog import ToolInfo, tool_catalog
from tools.deps import RunDeps
from tools.toolsets import build_agent_toolsets

from ._helpers import full_tool_categories

OWNER = "operator"

_CATEGORIES: dict[WorkspaceKind, set[str]] = {"sandbox": {"code"}, "worktree": {"shell", "repo"}}


def _catalog() -> list[ToolInfo]:
    return tool_catalog(full_tool_categories())


class TestTheNamesAreReal:
    def test_every_listed_name_exists_in_the_catalog(self):
        names = {t.name for t in _catalog()}
        for kind, listed in WORKSPACE_TOOLS.items():
            assert listed <= names, f"{kind} names a tool the catalog does not have"

    def test_each_kind_lists_every_tool_its_categories_register(self):
        for kind, categories in _CATEGORIES.items():
            registered = {t.name for t in _catalog() if t.category in categories}
            assert registered == WORKSPACE_TOOLS[kind], f"{kind} has drifted from the catalog"


class TestTheFilter:
    def test_a_sandbox_hides_the_worktree_tools(self):
        assert workspace_disabled_tools("sandbox") == WORKSPACE_TOOLS["worktree"]

    def test_a_worktree_hides_the_sandbox_runner(self):
        assert workspace_disabled_tools("worktree") == WORKSPACE_TOOLS["sandbox"]

    def test_an_unknown_value_is_treated_as_a_sandbox(self):
        assert workspace_kind("code") == "sandbox"
        assert workspace_kind(None) == "sandbox"
        assert workspace_disabled_tools("nonsense") == WORKSPACE_TOOLS["worktree"]


async def _offered(kind: str) -> set[str]:
    run = Run(id="t", kind="chat", owner_id=OWNER, stream=RunStream())
    deps = RunDeps(run=run, owner_id=OWNER, workspace_kind=workspace_kind(kind))
    ctx = RunContext(deps=deps, model=TestModel(), usage=RunUsage())
    return set(await build_agent_toolsets(full_tool_categories())[0].get_tools(ctx))


class TestTheGate:
    async def test_the_stack_offers_each_kind_its_own_tools(self):
        sandbox = await _offered("sandbox")
        worktree = await _offered("worktree")
        assert "code_execute" in sandbox and "shell_run_command" not in sandbox
        assert "shell_run_command" in worktree and "code_execute" not in worktree
        assert {"memory_recall"} <= sandbox & worktree

    async def test_the_gate_follows_the_kind_when_it_moves_mid_run(self):
        run = Run(id="t", kind="chat", owner_id=OWNER, stream=RunStream())
        deps = RunDeps(run=run, owner_id=OWNER)
        ctx = RunContext(deps=deps, model=TestModel(), usage=RunUsage())
        stack = build_agent_toolsets(full_tool_categories())[0]
        assert "shell_run_command" not in await stack.get_tools(ctx)
        deps.workspace_kind = "worktree"
        assert "shell_run_command" in await stack.get_tools(ctx)
