"""The workspace-kind filter, and the tripwire under its literal tool names.

`services/workspace_kind.py` names each kind's tools as literal strings, because `tools/`
sits above `services/` and importing the catalog there would invert the dependency order.
A literal set rots silently, so the names are checked against the catalog a real run
resolves. Every thread has the same home, so the shell and the code tools are offered in
all of them; only the repo inventory belongs to a project thread.
"""

from __future__ import annotations

from pydantic_ai import RunContext
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage

from runs import Run, RunStream
from services.workspace_kind import (
    WORKSPACE_TOOLS,
    workspace_disabled_tools,
    workspace_kind,
)
from tools.catalog import ToolInfo, tool_catalog
from tools.deps import RunDeps
from tools.toolsets import build_agent_toolsets

from ._helpers import full_tool_categories

OWNER = "operator"
INVENTORY = "repo_inventory_agent_context"


def _catalog() -> list[ToolInfo]:
    return tool_catalog(full_tool_categories())


class TestTheNamesAreReal:
    def test_every_listed_name_exists_in_the_catalog(self):
        names = {t.name for t in _catalog()}
        for kind, listed in WORKSPACE_TOOLS.items():
            assert listed <= names, f"{kind} names a tool the catalog does not have"

    def test_a_project_thread_lists_every_tool_the_repo_category_registers(self):
        registered = {t.name for t in _catalog() if t.category == "repo"}
        assert registered == WORKSPACE_TOOLS["worktree"] == WORKSPACE_TOOLS["checkout"]
        assert WORKSPACE_TOOLS["sandbox"] == frozenset()


class TestTheFilter:
    def test_a_sandbox_thread_hides_the_repo_inventory(self):
        assert workspace_disabled_tools("sandbox") == {INVENTORY}

    def test_a_project_thread_hides_nothing(self):
        assert workspace_disabled_tools("worktree") == frozenset()

    def test_an_unknown_value_is_treated_as_a_sandbox(self):
        assert workspace_kind("code") == "sandbox"
        assert workspace_kind(None) == "sandbox"
        assert workspace_disabled_tools("nonsense") == {INVENTORY}


async def _offered(kind: str) -> set[str]:
    run = Run(id="t", kind="chat", owner_id=OWNER, stream=RunStream())
    deps = RunDeps(run=run, owner_id=OWNER, workspace_kind=workspace_kind(kind))
    ctx = RunContext(deps=deps, model=TestModel(), usage=RunUsage())
    return set(await build_agent_toolsets(full_tool_categories())[0].get_tools(ctx))


class TestTheGate:
    async def test_every_thread_has_the_shell_and_the_code_tools(self):
        sandbox = await _offered("sandbox")
        worktree = await _offered("worktree")
        assert {"code_execute", "shell_run_command", "files_write_file"} <= sandbox & worktree
        assert INVENTORY in worktree and INVENTORY not in sandbox

    async def test_the_gate_follows_the_kind_when_it_moves_mid_run(self):
        run = Run(id="t", kind="chat", owner_id=OWNER, stream=RunStream())
        deps = RunDeps(run=run, owner_id=OWNER)
        ctx = RunContext(deps=deps, model=TestModel(), usage=RunUsage())
        stack = build_agent_toolsets(full_tool_categories())[0]
        assert INVENTORY not in await stack.get_tools(ctx)
        deps.workspace_kind = "worktree"
        assert INVENTORY in await stack.get_tools(ctx)
