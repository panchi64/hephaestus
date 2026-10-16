"""Landing the work of a sub-agent spawned into a worktree of its own.

The sub-agent's branch waits until the agent that spawned it decides. Merging lands it
where that agent works: a git merge into its own worktree, or, when it works in the
project folder, the sub-agent's changes applied there as file changes, recorded like
any other write so the operator can put them back.
"""

from __future__ import annotations

from pathlib import Path

from pydantic_ai import ModelRetry, RunContext

from core.diagnostics import render_tool_error
from core.fork import MergeReport
from services.home.paths import Place
from services.home.table import child_worktree_name
from services.projects.repo import WorktreeError
from services.projects.store import ProjectStore
from services.projects.worktree import WorktreeBusyError, WorktreeManager
from services.subagents import ChildWorktree, SubagentLauncher, SubagentUnavailableError

from .deps import RunDeps
from .home import change_log, resolve_run_home
from .workspace import resolve_run_workspace

_UNAVAILABLE = "Worktrees are unavailable in this deployment."

#: The statuses of a sub-agent that is still writing to its worktree.
_WORKING = frozenset({"running", "blocked"})


async def worktree_paths(ctx: RunContext[RunDeps]) -> dict[str, dict[str, str]]:
    """Each open worktree of a sub-agent this thread spawned, by handle: its path in the
    home and its branch."""
    launcher = ctx.deps.caps.get_optional(SubagentLauncher)
    projects = ctx.deps.caps.get_optional(ProjectStore)
    if launcher is None or projects is None or not ctx.deps.conversation_id:
        return {}
    children = await launcher.worktrees(ctx.deps.owner_id, ctx.deps.conversation_id)
    if not children:
        return {}
    slugs = await projects.slugs(ctx.deps.owner_id)
    return {
        child.handle: {
            "worktree": Place(
                "worktrees", child_worktree_name(slugs.get(child.project_id, ""), child.handle)
            ).path,
            "branch": child.branch,
        }
        for child in children
        if slugs.get(child.project_id)
    }


async def merge_child(ctx: RunContext[RunDeps], name: str) -> dict[str, object]:
    """Land a sub-agent's worktree where this thread works, and retire it."""
    child = await _settled_child(ctx, name)
    worktrees = ctx.deps.caps.get_optional(WorktreeManager)
    projects = ctx.deps.caps.get_optional(ProjectStore)
    if worktrees is None or projects is None:
        return {"merged": False, "detail": _UNAVAILABLE}
    project = await projects.get(ctx.deps.owner_id, child.project_id)
    root = Path(project.root_path)
    try:
        if ctx.deps.worktree and ctx.deps.project_id == child.project_id:
            report, into = await _merge_into_worktree(ctx, worktrees, root, child)
        else:
            slug = (await projects.slugs(ctx.deps.owner_id)).get(child.project_id, "")
            report, into = await _apply_to_checkout(ctx, worktrees, root, child, slug)
    except (WorktreeError, WorktreeBusyError) as exc:
        raise ModelRetry(
            render_tool_error(
                f"could not merge `{child.handle}`: {exc}",
                hints=["its worktree is untouched; fix the cause there and merge again"],
            )
        ) from exc
    if report.merged:
        ctx.deps.home = None
    return _merged(child, report, into)


async def discard_child(ctx: RunContext[RunDeps], name: str) -> dict[str, object]:
    """Throw a sub-agent's worktree and branch away."""
    child = await _settled_child(ctx, name)
    worktrees = ctx.deps.caps.get_optional(WorktreeManager)
    projects = ctx.deps.caps.get_optional(ProjectStore)
    if worktrees is None or projects is None:
        return {"discarded": False, "detail": _UNAVAILABLE}
    project = await projects.get(ctx.deps.owner_id, child.project_id)
    await worktrees.discard_child(
        project_id=child.project_id,
        root=Path(project.root_path),
        conversation_id=child.holder_id,
        delegation_id=child.delegation_id,
    )
    ctx.deps.home = None
    return {"discarded": True, "name": child.handle, "branch": child.branch}


async def _settled_child(ctx: RunContext[RunDeps], name: str) -> ChildWorktree:
    """The sub-agent's open worktree, once it has stopped writing to it."""
    launcher = ctx.deps.caps.get_optional(SubagentLauncher)
    if launcher is None:
        raise ModelRetry(
            render_tool_error(
                "delegation is unavailable in this deployment",
                hints=["work in the project yourself; there is no sub-agent to merge"],
            )
        )
    conversation_id = ctx.deps.conversation_id or ""
    try:
        child = await launcher.worktree(ctx.deps.owner_id, conversation_id, name)
        view = await launcher.read(ctx.deps.owner_id, conversation_id, name)
    except SubagentUnavailableError as exc:
        hints = ["call agents_list for your agents and their worktrees"]
        raise ModelRetry(render_tool_error(str(exc), hints=hints)) from exc
    if view.status in _WORKING:
        raise ModelRetry(
            render_tool_error(
                f"`{child.handle}` is still working in its worktree",
                hints=["wait for its report, or stop it with agents_stop first"],
            )
        )
    return child


async def _merge_into_worktree(
    ctx: RunContext[RunDeps], worktrees: WorktreeManager, root: Path, child: ChildWorktree
) -> tuple[MergeReport, str]:
    workspace = await resolve_run_workspace(ctx.deps)
    if workspace is None:
        raise WorktreeError("your worktree could not be opened")
    report = await worktrees.merge_child(
        project_id=child.project_id,
        root=root,
        conversation_id=child.holder_id,
        delegation_id=child.delegation_id,
        into=workspace.root,
    )
    return report, workspace.home_path or str(workspace.root)


async def _apply_to_checkout(
    ctx: RunContext[RunDeps],
    worktrees: WorktreeManager,
    root: Path,
    child: ChildWorktree,
    slug: str,
) -> tuple[MergeReport, str]:
    place = Place("projects", slug)
    home = await resolve_run_home(ctx.deps)
    mount = home.mount(place) if home is not None and slug else None
    if mount is None or not mount.writable:
        raise ModelRetry(
            render_tool_error(
                f"{place.path} is read-only to you, so `{child.handle}`'s work cannot land there",
                hints=["its worktree stays open; say in your answer what it holds"],
            )
        )
    changed = await worktrees.child_changes(
        project_id=child.project_id,
        conversation_id=child.holder_id,
        delegation_id=child.delegation_id,
        base_ref=child.base_ref,
    )
    paths = [*changed.files, *changed.deleted]
    log = change_log(ctx.deps)
    if log is not None:
        await log.before(mount, paths)
    report = await worktrees.apply_child(
        project_id=child.project_id,
        root=root,
        conversation_id=child.holder_id,
        delegation_id=child.delegation_id,
        base_ref=child.base_ref,
    )
    if log is not None:
        await log.after(mount, paths)
    return report, place.path


def _merged(child: ChildWorktree, report: MergeReport, into: str) -> dict[str, object]:
    if not report.merged:
        return {
            "merged": False,
            "name": child.handle,
            "conflicts": report.conflicts,
            "detail": render_tool_error(
                f"`{child.handle}`'s changes clash with {into}",
                cause="these files changed on both sides: " + ", ".join(report.conflicts),
                hints=[
                    "nothing was changed; edit its worktree to match yours, then merge again",
                    "or make the change yourself and agents_discard it",
                ],
            ),
        }
    return {
        "merged": True,
        "name": child.handle,
        "into": into,
        "files": report.files,
        "deleted": report.deleted,
    }
