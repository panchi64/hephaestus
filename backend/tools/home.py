"""The run's home, as a tool and the approval gate see it.

Assembled once per run and memoised on its deps: the thread's scratch (the sandbox
session's workspace), every registered project, writable where the thread holds a lease
or was started in it, the thread's worktree of its project when it opened one, and the
open worktrees of the sub-agents it spawned.
"""

from __future__ import annotations

import logging
from pathlib import Path

from services.home.changes import ChangeLog, ThreadChanges
from services.home.leases import WriteLeases
from services.home.paths import Place
from services.home.table import RunHome, default_place, thread_mounts
from services.projects.store import ProjectStore
from services.sandbox import SandboxError, SandboxSessionManager
from services.subagents import SubagentLauncher
from services.workspace import sandbox_workspace, split_workspace_key

from .deps import RunDeps
from .workspace import resolve_run_workspace

logger = logging.getLogger(__name__)


async def resolve_run_home(deps: RunDeps) -> RunHome | None:
    """This run's home, or None when it holds no place at all.

    Raises ``WorktreeBusyError`` when the thread's worktree is held elsewhere, which the
    operator has to be told rather than given a home without the project.
    """
    if deps.home is not None:
        return deps.home
    projects = deps.caps.get_optional(ProjectStore)
    scratch = None
    sessions = deps.caps.get_optional(SandboxSessionManager)
    if sessions is not None:
        try:
            scratch = (await sandbox_workspace(sessions, deps.workspace_key, holder=deps.run)).root
        except SandboxError:
            logger.debug("home: no scratch for %s", deps.workspace_key, exc_info=True)
    slug = None
    worktree_root: Path | None = None
    if deps.project_id and projects is not None:
        slug = (await projects.slugs(deps.owner_id)).get(deps.project_id)
        if deps.worktree:
            workspace = await resolve_run_workspace(deps)
            worktree_root = workspace.root if workspace is not None else None
    mounts = await thread_mounts(
        owner_id=deps.owner_id,
        scratch=scratch,
        projects=projects,
        leases=deps.caps.get_optional(WriteLeases),
        lease_holder=lease_holder(deps),
        filed_project_id=deps.project_id,
        # A thread on a worktree leaves the operator's working copy alone, and so does a
        # spawned agent whose parent works on one.
        filed_writable=not deps.worktree and not deps.checkout_read_only,
        worktree_root=worktree_root,
        child_worktrees=await _child_worktrees(deps),
    )
    default = default_place(mounts, slug)
    if default is None:
        return None
    deps.home = RunHome(mounts=tuple(mounts), default=default, key=deps.workspace_key)
    return deps.home


async def _child_worktrees(deps: RunDeps) -> list[tuple[str, str, Path]]:
    launcher = deps.caps.get_optional(SubagentLauncher)
    if launcher is None or not deps.conversation_id:
        return []
    children = await launcher.worktrees(deps.owner_id, deps.conversation_id)
    return [(child.project_id, child.handle, child.path) for child in children]


def ledger_thread(deps: RunDeps) -> str | None:
    """The thread whose ledger this run's changes are filed in: the one whose files it works
    in, so a spawned agent's land in its parent's list."""
    owner, _ = split_workspace_key(deps.workspace_key)
    return owner or deps.conversation_id


def ledger_prompt(deps: RunDeps) -> str | None:
    """The prompt this run's changes are filed under in :func:`ledger_thread`: its own turn's
    when it works in its own thread, else the launching turn's."""
    if ledger_thread(deps) == deps.conversation_id:
        return deps.run.prompt_message_id
    return deps.ledger_prompt_message_id


def change_log(deps: RunDeps) -> ChangeLog | None:
    """Where this run's changes to the operator's project folders are recorded."""
    store = deps.caps.get_optional(ThreadChanges)
    thread = ledger_thread(deps)
    if store is None or not thread:
        return None
    prompt = ledger_prompt(deps)
    return ChangeLog(
        store, owner_id=deps.owner_id, conversation_id=thread, prompt_message_id=prompt
    )


def lease_holder(deps: RunDeps) -> str | None:
    """The conversation whose leases this run works under, or None for none."""
    if deps.leases_from is not None:
        return deps.leases_from or None
    return deps.conversation_id


def may_lease(deps: RunDeps) -> bool:
    """Whether an approval in this run can give its thread a new lease: a spawned agent
    works within what its parent holds."""
    return deps.leases_from is None and not deps.delegated_approved and bool(deps.conversation_id)


async def lease(deps: RunDeps, home: RunHome, place: Place) -> RunHome:
    """Let this thread write ``place`` from now on, and the rest of this run at once."""
    leases = deps.caps.get_optional(WriteLeases)
    if leases is not None and deps.conversation_id:
        await leases.lease(deps.owner_id, deps.conversation_id, place)
    deps.home = home.with_writable(place)
    return deps.home
