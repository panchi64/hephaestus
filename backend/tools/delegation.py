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
from pydantic_ai.toolsets import ToolsetTool, WrapperToolset

from core.config import get_settings
from core.diagnostics import render_tool_error
from services.agents import (
    AgentDefinition,
    AgentNameError,
    builtin_roster,
    delegable,
    describe_roster,
    normalize_agent_name,
)
from services.subagents import (
    ROOT_HANDLE,
    SubagentLauncher,
    SubagentParent,
    SubagentUnavailableError,
    SubagentView,
)
from services.subagents.callsigns import class_display
from tools.child_worktrees import discard_child, merge_child, worktree_paths
from tools.deps import RunDeps
from tools.describe import registered_tools
from tools.project_agents import run_roster

#: The spawn tool's name inside the category, before the prefix makes it ``agents_spawn``.
SPAWN_TOOL = "spawn"

_UNAVAILABLE = "Delegation is unavailable in this deployment."

#: The fewest words a task can say what to do, where to start and what done looks like in.
_MIN_TASK_WORDS = 15

_SPAWN_DOC = """Spawn an agent to do a self-contained piece of work on its own.

{roster}

It returns immediately; never poll. Spawn as many as the work splits into, issuing the calls
together, carry on, and end your turn when nothing is left: each one's report reaches you as
it finishes. Not for work you could do in a couple of tool calls. It may do whatever you may.

`name` is your handle for it — the job, not the worker (`auth-route-reviewer`). `task` is
its first message and stands alone: the agent never sees this conversation, so say what to
do, where to start, and what a finished answer looks like.

`worktree` gives it its own branch and worktree of your project, so you can keep editing
meanwhile. Its work waits there for you to review, fix, and `agents_merge` or discard.
"""


def add_delegation_tools(toolset: FunctionToolset[RunDeps]) -> None:
    """Register the delegation tools on the `agents` category's toolset."""

    async def spawn(
        ctx: RunContext[RunDeps],
        name: str,
        agent_name: str,
        task: str,
        worktree: bool = False,
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
        if len(task.split()) < _MIN_TASK_WORDS:
            raise ModelRetry(
                render_tool_error(
                    f"the task for `{name}` is too short to work from",
                    hints=[
                        "the agent never sees this conversation: say what to do, where to "
                        + "start (files, URLs, names), and what a finished answer looks like"
                    ],
                )
            )
        try:
            started = await launcher.launch(
                ctx.deps.owner_id,
                agent,
                task,
                handle=name,
                parent=_parent(ctx),
                worktree=worktree,
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
            "callsign": started.callsign,
            "class": class_display(started.agent_class),
            "agent_name": started.name,
            "detail": "Running on its own; its report will reach you." + renamed,
        }

    spawn.__doc__ = spawn_description(delegable(builtin_roster()))
    _ = toolset.add_function(spawn, name=SPAWN_TOOL)

    @toolset.tool(name="message")
    async def message(ctx: RunContext[RunDeps], to: str, text: str) -> dict[str, object]:
        """Send a message to another agent of your delegation tree: a handle or callsign
        from `agents_list`, `parent` for the agent that spawned you, or `main` for the thread at
        the top. It reaches a running agent at its next step and wakes an idle one; a reply,
        if any, arrives as a message."""
        launcher = ctx.deps.caps.get_optional(SubagentLauncher)
        if launcher is None:
            return {"sent": False, "detail": _UNAVAILABLE}
        conversation_id = ctx.deps.conversation_id or ""
        try:
            label = await launcher.message(ctx.deps.owner_id, conversation_id, to, text)
        except SubagentUnavailableError as exc:
            return {"sent": False, "detail": str(exc)}
        return {"sent": True, "to": label}

    @toolset.tool(name="merge")
    async def merge(ctx: RunContext[RunDeps], name: str) -> dict[str, object]:
        """Land the work of an agent you spawned with `worktree` where you work. Review and
        fix it in its worktree first. A clash changes nothing and names the files."""
        return await merge_child(ctx, name)

    @toolset.tool(name="discard")
    async def discard(ctx: RunContext[RunDeps], name: str) -> dict[str, object]:
        """Throw away the worktree and branch of an agent you spawned with `worktree`."""
        return await discard_child(ctx, name)

    @toolset.tool(name="list")
    async def list_agents(ctx: RunContext[RunDeps]) -> dict[str, object]:
        """Every agent of your delegation tree, with who spawned it, whether it is still
        working, and the worktree of each you spawned into one. Check it before concluding
        while agents you spawned are still out."""
        launcher = ctx.deps.caps.get_optional(SubagentLauncher)
        if launcher is None:
            return {"available": False, "detail": _UNAVAILABLE}
        conversation_id = ctx.deps.conversation_id or ""
        views = await launcher.tree(ctx.deps.owner_id, conversation_id)
        handles = {view.conversation_id: view.handle for view in views}
        opened = await worktree_paths(ctx)
        return {
            "available": True,
            "you": handles.get(conversation_id, ROOT_HANDLE),
            "agents": [
                {
                    "name": view.handle,
                    "callsign": view.callsign,
                    "class": class_display(view.agent_class),
                    "agent_name": view.name,
                    "parent": handles.get(view.parent_conversation_id, ROOT_HANDLE),
                    "task": view.task,
                    "status": view.status,
                    **opened.get(view.handle, {}),
                }
                for view in views
            ],
        }

    @toolset.tool(name="read")
    async def read(ctx: RunContext[RunDeps], name: str, start: int = 0) -> dict[str, object]:
        """An agent of your tree: its status, and its report, or its latest answer while it
        works. A long report comes in pages: pass `next_start` as `start` for the next."""
        launcher = ctx.deps.caps.get_optional(SubagentLauncher)
        if launcher is None:
            return {"available": False, "detail": _UNAVAILABLE}
        try:
            view = await launcher.read(ctx.deps.owner_id, ctx.deps.conversation_id or "", name)
        except SubagentUnavailableError as exc:
            raise ModelRetry(f"could not read {name!r}:\n{exc}") from exc
        return {
            **_paged(_read_out(view), start),
            **(await worktree_paths(ctx)).get(view.handle, {}),
        }

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


def _paged(out: dict[str, object], start: int) -> dict[str, object]:
    """One page of the report, sized to stay under the tool-output spill so the whole of
    it reaches the model; ``next_start`` is there while more remains."""
    report = out.get("report")
    page = get_settings().tool_output_spill_chars // 2
    if not isinstance(report, str) or (start <= 0 and len(report) <= page):
        return out
    start = max(start, 0)
    paged = {**out, "report": report[start : start + page], "report_length": len(report)}
    if start + page < len(report):
        paged["next_start"] = start + page
    return paged


def _read_out(view: SubagentView) -> dict[str, object]:
    return {
        "available": True,
        "name": view.handle,
        "callsign": view.callsign,
        "class": class_display(view.agent_class),
        "agent_name": view.name,
        "status": view.status,
        "report": view.summary,
        "error": view.error,
    }


def _parent(ctx: RunContext[RunDeps]) -> SubagentParent:
    """What the spawning thread hands the agent: its project, its level as a ceiling, and
    the workspace key that decides which files it works in."""
    return SubagentParent(
        conversation_id=ctx.deps.conversation_id,
        project_id=ctx.deps.project_id,
        worktree=ctx.deps.worktree,
        permission=ctx.deps.permission,
        workspace_key=ctx.deps.workspace_key,
        run_id=ctx.deps.run.id,
    )
