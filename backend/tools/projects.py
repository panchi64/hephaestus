"""The `project` category — what the agent is working in.

Two read-only tools answer questions the model would otherwise guess at: *what projects
exist* and *which one is this conversation in*. The third, ``open_worktree``, is the
agent's own call to open a worktree of its project, a branch of its own beside the
operator's folder. Creating or binding a project is the operator's act, so there is no
tool for it.

The active project comes from ``ctx.deps.project_id``, which the chat layer resolves from
the **conversation's** binding rather than the live request: a run must keep reporting the
project it started in even if the operator switches away mid-turn.
"""

from __future__ import annotations

from pydantic_ai import FunctionToolset, RunContext

from services.home.paths import Place
from services.projects import ProjectStore
from services.projects.worktree import WorktreeBusyError
from services.worktree_switch import WorktreeSwitch
from tools.deps import RunDeps
from tools.workspace import run_workspace, unavailable

_UNAVAILABLE = "Projects are unavailable in this deployment."


def project_toolset() -> FunctionToolset[RunDeps]:
    toolset = FunctionToolset[RunDeps]()

    # Named explicitly rather than by the function name: the namespaced tool the model
    # sees is `project_list`, and shadowing the `list` builtin to get there would be a
    # poor trade for a name the decorator can simply be told.
    @toolset.tool(name="list")
    async def list_projects(ctx: RunContext[RunDeps]) -> dict[str, object]:
        """List the operator's projects — the directories they work in.

        Returns each project's name and its path in your home, and which one (if any)
        this conversation is filed under.
        """
        store = ctx.deps.caps.get_optional(ProjectStore)
        if store is None:
            return {"available": False, "detail": _UNAVAILABLE}
        views = await store.list(ctx.deps.owner_id)
        slugs = await store.slugs(ctx.deps.owner_id)
        return {
            "available": True,
            "active_project_id": ctx.deps.project_id,
            "projects": [
                {
                    "id": v.id,
                    "name": v.name,
                    "path": Place("projects", slugs.get(v.id, "")).path,
                    "is_git_repo": v.probe.is_git_repo,
                    "uncommitted_changes": v.probe.uncommitted_changes,
                }
                for v in views
            ],
        }

    @toolset.tool(name="active")
    async def active_project(ctx: RunContext[RunDeps]) -> dict[str, object]:
        """The project this conversation is working in, if any.

        In a worktree this is the project whose git worktree your file and shell tools
        act on. Uncommitted changes in the operator's own checkout are **not** visible to
        you — your worktree branches from the project's base ref — so if their tree is
        dirty, say so rather than assuming you can see their latest edits.
        """
        store = ctx.deps.caps.get_optional(ProjectStore)
        if store is None:
            return {"available": False, "detail": _UNAVAILABLE}
        if ctx.deps.project_id is None:
            return {"available": True, "project": None, "worktree": False}
        view = await store.get(ctx.deps.owner_id, ctx.deps.project_id)
        slug = (await store.slugs(ctx.deps.owner_id)).get(view.id, "")
        return {
            "available": True,
            "worktree": ctx.deps.worktree,
            "project": {
                "id": view.id,
                "name": view.name,
                "path": Place("projects", slug).path,
                **({"worktree_path": Place("worktrees", slug).path} if ctx.deps.worktree else {}),
                "base_ref": view.base_ref,
                "is_git_repo": view.probe.is_git_repo,
                "uncommitted_changes": view.probe.uncommitted_changes,
            },
        }

    @toolset.tool(name="open_worktree")
    async def open_worktree(ctx: RunContext[RunDeps]) -> str:
        """Open a git worktree of this conversation's project: a separate checkout on a
        branch of its own, at `~/worktrees/<name>`, which reaches the operator's folder only
        when they merge it.

        Use it before work that should stay apart from their working copy: a risky or
        sprawling change, or one that would collide with what they are doing. Small edits
        belong in their folder. From then on relative paths land in the worktree and their
        folder is read-only to you. The worktree branches from the project's base ref, so
        uncommitted changes in their folder (including edits you made there) do not come
        with you.
        """
        deps = ctx.deps
        if deps.worktree:
            return "This conversation already works in a worktree."
        if deps.project_id is None or deps.conversation_id is None:
            return "This conversation is not filed under a project, so there is nothing to open."
        if deps.workspace_key != deps.conversation_id:
            return "A delegated agent works in its parent's workspace and cannot move it."
        switch = deps.caps.get_optional(WorktreeSwitch)
        if switch is None or not await switch.open(deps.conversation_id):
            return "Worktrees are unavailable in this deployment."
        deps.worktree = True
        deps.workspace = None
        deps.home = None
        try:
            workspace = await run_workspace(ctx)
        except WorktreeBusyError as exc:
            return f"Recorded the worktree, but it is busy: {exc}"
        if workspace is None:
            return unavailable(deps)
        where = workspace.display("").rstrip("/")
        return (
            f"Opened a worktree on branch `{workspace.branch}` at {where}. Relative paths in "
            "the shell and file tools land there from your next step; `cd` there to run "
            "commands in it."
        )

    return toolset
