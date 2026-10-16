"""The run's home, as a tool and the approval gate see it.

Assembled once per run and memoised on its deps: the thread's scratch (the sandbox
session's workspace), every registered project, writable where the thread holds a lease
or was started in it, and the thread's worktree of its project when it opened one.
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
        # A thread on a worktree leaves the operator's working copy alone, and a spawned
        # agent working apart writes only its own copies.
        filed_writable=not deps.worktree and deps.leases_from != "",
        worktree_root=worktree_root,
    )
    default = default_place(mounts, slug)
    if default is None:
        return None
    deps.home = RunHome(mounts=tuple(mounts), default=default, key=deps.workspace_key)
    return deps.home


def change_log(deps: RunDeps) -> ChangeLog | None:
    """Where this run's changes to the operator's project folders are recorded: under the
    thread whose files it works in, so a spawned agent's land in its parent's list."""
    store = deps.caps.get_optional(ThreadChanges)
    owner, _ = split_workspace_key(deps.workspace_key)
    thread = owner or deps.conversation_id
    if store is None or not thread:
        return None
    return ChangeLog(store, owner_id=deps.owner_id, conversation_id=thread)


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
