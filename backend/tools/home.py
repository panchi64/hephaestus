"""The run's home, as a tool and the approval gate see it.

Assembled once per run and memoised on its deps: the thread's scratch (the sandbox
session's workspace), and every registered project, writable where the thread holds a
lease or was started in it. A project thread's own project is mounted on the folder it
works in, which for a worktree thread is the worktree.
"""

from __future__ import annotations

import logging

from services.home.leases import WriteLeases
from services.home.paths import Place
from services.home.table import RunHome, default_place, thread_mounts
from services.projects.store import ProjectStore
from services.sandbox import SandboxError, SandboxSessionManager
from services.workspace import sandbox_workspace
from services.workspace_kind import is_project_folder

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
    filed_root = None
    filed: Place | None = None
    if is_project_folder(deps.workspace_kind) and deps.project_id and projects is not None:
        workspace = await resolve_run_workspace(deps)
        if workspace is not None:
            filed_root = workspace.root
            slug = (await projects.slugs(deps.owner_id)).get(deps.project_id)
            filed = Place("projects", slug) if slug else None
    mounts = await thread_mounts(
        owner_id=deps.owner_id,
        conversation_id=deps.conversation_id,
        scratch=scratch,
        projects=projects,
        leases=deps.caps.get_optional(WriteLeases),
        filed_project_id=deps.project_id if filed_root is not None else None,
        filed_root=filed_root,
    )
    default = default_place(mounts, filed)
    if default is None:
        return None
    deps.home = RunHome(mounts=tuple(mounts), default=default, key=deps.workspace_key)
    return deps.home


async def lease(deps: RunDeps, home: RunHome, place: Place) -> RunHome:
    """Let this thread write ``place`` from now on, and the rest of this run at once."""
    leases = deps.caps.get_optional(WriteLeases)
    if leases is not None and deps.conversation_id:
        await leases.lease(deps.owner_id, deps.conversation_id, place)
    deps.home = home.with_writable(place)
    return deps.home
