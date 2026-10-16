"""Delegation tools in the `agents` category — spawning agents and working with the tree.

No tool here waits on another run. ``spawn`` returns once the agent's first turn is
submitted, and messages are queued or delivered by waking the recipient; results arrive as
wakes. So a parent can never block on its own child, whatever the endpoint queue holds.

The spawn tool's roster is generated per run, from the agents that run can see — built-ins,
the project's files and published stored agents — because that is where the model chooses.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import override

from pydantic_ai import FunctionToolset, ModelRetry, RunContext, Tool
from pydantic_ai.exceptions import ApprovalRequired
from pydantic_ai.toolsets import ToolsetTool, WrapperToolset

from core.diagnostics import render_tool_error
from services.agents import (
    AgentDefinition,
    AgentNameError,
    builtin_roster,
    delegable,
    describe_roster,
    normalize_agent_name,
)
from services.permissions import STRICTEST_PERMISSION
from services.subagents import (
    ROOT_HANDLE,
    SubagentLauncher,
    SubagentParent,
    SubagentUnavailableError,
    SubagentView,
)
from tools.deps import RunDeps
from tools.describe import registered_tools
from tools.project_agents import run_catalog, run_roster

#: The spawn tool's name inside the category, before the prefix makes it ``agents_spawn``.
SPAWN_TOOL = "spawn"

#: Raised for approval only when the agent being spawned, or sent a message from anywhere
#: but below it, can write.
GATED_TOOLS: frozenset[str] = frozenset({f"agents_{SPAWN_TOOL}", "agents_message"})

_UNAVAILABLE = "Delegation is unavailable in this deployment."

_SPAWN_DOC = """Spawn an agent to do a self-contained piece of work on its own.

{roster}

It returns immediately; never poll. Spawn as many as the work splits into, issuing the calls
together, carry on, and end your turn when nothing is left: each one's report reaches you as
it finishes. Not for work you could do in a couple of tool calls.

`name` is your handle for it — the job, not the worker (`auth-route-reviewer`). `task`
stands alone: the agent never sees this conversation, so say what to do, where to start, and
what a finished answer looks like. `isolate` gives it a copy of your workspace, merged back
when it finishes; ask for it when you will keep editing meanwhile.
"""


def add_delegation_tools(toolset: FunctionToolset[RunDeps]) -> None:
    """Register the delegation tools on the `agents` category's toolset."""

    async def spawn(
        ctx: RunContext[RunDeps],
        name: str,
        agent_name: str,
        task: str,
        isolate: bool = False,
    ) -> dict[str, object]:
        launcher = ctx.deps.caps.get_optional(SubagentLauncher)
        if launcher is None:
            return {"spawned": False, "detail": _UNAVAILABLE}
        try:
            name = normalize_agent_name(name)
        except AgentNameError as exc:
            raise ModelRetry(f"{exc} Name it for the job, like `helper-function-finder`.") from exc
        roster = await run_roster(ctx)
        agent = roster.get(agent_name)
        if agent is None:
            raise ModelRetry(
                f"There is no agent called {agent_name!r}. Choose one of: "
                + f"{', '.join(sorted(roster))}."
            )
        # Before a workspace is forked for it: a launch that ends in edits is the
        # operator's to allow.
        if can_write(agent) and not ctx.tool_call_approved:
            raise ApprovalRequired()
        try:
            started = await launcher.launch(
                ctx.deps.owner_id,
                agent,
                task,
                handle=name,
                parent=_parent(ctx),
                isolate=isolate,
            )
        except SubagentUnavailableError as exc:
            return {"spawned": False, "detail": str(exc)}
        renamed = (
            ""
            if started.handle == name
            else f" Named `{started.handle}`, because `{name}` was taken in this tree."
        )
        return {
            "spawned": True,
            "name": started.handle,
            "agent_name": started.name,
            "detail": "Running on its own; its report will reach you." + renamed,
        }

    spawn.__doc__ = spawn_description(delegable(builtin_roster()))
    # Not `requires_approval=True`: that defers every call before the body runs, while
    # only an agent that can write is gated.
    _ = toolset.add_function(spawn, name=SPAWN_TOOL)

    @toolset.tool(name="message")
    async def message(ctx: RunContext[RunDeps], to: str, text: str) -> dict[str, object]:
        """Send a message to another agent of your delegation tree: a handle from
        `agents_list`, `parent` for the agent that spawned you, or `main` for the thread at
        the top. It reaches a running agent at its next step and wakes an idle one; you are
        not waiting on a reply, which arrives as a message of its own if one comes. A message
        to an agent that can write, unless it is above you, asks the operator first."""
        launcher = ctx.deps.caps.get_optional(SubagentLauncher)
        if launcher is None:
            return {"sent": False, "detail": _UNAVAILABLE}
        conversation_id = ctx.deps.conversation_id or ""
        try:
            recipient = await launcher.recipient(ctx.deps.owner_id, conversation_id, to)
            # Up the tree a message is an early report, which every agent may send. Across
            # or down it directs work, so reaching an agent that can write asks first, as
            # spawning one does: a read-only agent's message can carry what it read.
            if not recipient.upward and not ctx.tool_call_approved:
                agent = (await run_catalog(ctx)).get(recipient.agent_name or "")
                if agent is None or can_write(agent):
                    raise ApprovalRequired()
            label = await launcher.message(ctx.deps.owner_id, conversation_id, to, text)
        except SubagentUnavailableError as exc:
            return {"sent": False, "detail": str(exc)}
        return {"sent": True, "to": label}

    @toolset.tool(name="list")
    async def list_agents(ctx: RunContext[RunDeps]) -> dict[str, object]:
        """Every agent of your delegation tree, with who spawned it and whether it is still
        working. Check it before concluding while agents you spawned are still out."""
        launcher = ctx.deps.caps.get_optional(SubagentLauncher)
        if launcher is None:
            return {"available": False, "detail": _UNAVAILABLE}
        conversation_id = ctx.deps.conversation_id or ""
        views = await launcher.tree(ctx.deps.owner_id, conversation_id)
        handles = {view.conversation_id: view.handle for view in views}
        return {
            "available": True,
            "you": handles.get(conversation_id, ROOT_HANDLE),
            "agents": [
                {
                    "name": view.handle,
                    "agent_name": view.name,
                    "parent": handles.get(view.parent_conversation_id, ROOT_HANDLE),
                    "task": view.task,
                    "status": view.status,
                }
                for view in views
            ],
        }

    @toolset.tool(name="read")
    async def read(ctx: RunContext[RunDeps], name: str) -> dict[str, object]:
        """An agent of your tree: its status, and its report, or its latest answer while it
        works. You do not need this to receive a report."""
        launcher = ctx.deps.caps.get_optional(SubagentLauncher)
        if launcher is None:
            return {"available": False, "detail": _UNAVAILABLE}
        try:
            view = await launcher.read(ctx.deps.owner_id, ctx.deps.conversation_id or "", name)
        except SubagentUnavailableError as exc:
            raise ModelRetry(
                render_tool_error(
                    f"could not read {name!r}: {exc}",
                    hints=["call agents_list_agents for the names in your tree"],
                )
            ) from exc
        return _read_out(view)

    @toolset.tool(name="stop")
    async def stop(ctx: RunContext[RunDeps], name: str) -> dict[str, object]:
        """Stop an agent you spawned, or one below it, along with everything it spawned.
        Each stopped agent reports what it got through."""
        launcher = ctx.deps.caps.get_optional(SubagentLauncher)
        if launcher is None:
            return {"stopped": [], "detail": _UNAVAILABLE}
        try:
            stopped = await launcher.stop(ctx.deps.owner_id, ctx.deps.conversation_id or "", name)
        except SubagentUnavailableError as exc:
            return {"stopped": [], "detail": str(exc)}
        return {"stopped": stopped}


def spawn_description(roster: Mapping[str, AgentDefinition]) -> str:
    """What the model is told it may spawn, for one roster."""
    return _SPAWN_DOC.format(roster=describe_roster(roster))


@dataclass
class ProjectRoster(WrapperToolset[RunDeps]):
    """The `agents` category, with the spawn tool described against the roster of the run
    reading it. Only the description moves; the name and schema are the same for every
    run, which keeps the static catalog honest."""

    @property
    @override
    def id(self) -> str:
        return "agents"

    @property
    def tools(self) -> Mapping[str, Tool[RunDeps]]:
        """The static registry the settings catalog enumerates."""
        return registered_tools(self.wrapped)

    @override
    async def get_tools(self, ctx: RunContext[RunDeps]) -> dict[str, ToolsetTool[RunDeps]]:
        tools = await super().get_tools(ctx)
        tool = tools.get(SPAWN_TOOL)
        if tool is None:
            return tools
        described = spawn_description(await run_roster(ctx))
        if described == tool.tool_def.description:
            # Identical object, so the request's prefix stays byte-for-byte stable.
            return tools
        return {
            **tools,
            SPAWN_TOOL: replace(tool, tool_def=replace(tool.tool_def, description=described)),
        }


def _read_out(view: SubagentView) -> dict[str, object]:
    return {
        "available": True,
        "name": view.handle,
        "agent_name": view.name,
        "status": view.status,
        "report": view.summary,
        "error": view.error,
    }


def can_write(agent: AgentDefinition) -> bool:
    """Whether spawning this agent is the operator's to rule on, read off its ceiling."""
    return agent.permission_ceiling != STRICTEST_PERMISSION


def _parent(ctx: RunContext[RunDeps]) -> SubagentParent:
    """What the spawning thread hands the agent. Every field narrows it: the permission
    is a ceiling, and the workspace key decides which files it works in."""
    return SubagentParent(
        conversation_id=ctx.deps.conversation_id,
        project_id=ctx.deps.project_id,
        worktree=ctx.deps.worktree,
        permission=ctx.deps.permission,
        workspace_key=ctx.deps.workspace_key,
        run_id=ctx.deps.run.id,
    )
