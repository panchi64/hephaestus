"""The agents this run can see — built-ins, the project's own files, and stored agents.

Read where the model is deciding: inside ``get_tools``, on every model request of the turn.
So it is cached per run, which also keeps an agent file edited mid-turn from rewriting the
launch tool's description under the turn's own prefix cache; the next turn re-reads.

A project's files are read from the thread's tree without resolving its workspace, so
describing a tool never takes the project's checkout or creates a worktree.
"""

from __future__ import annotations

from collections import OrderedDict
from pathlib import Path

from pydantic_ai import RunContext

from core.exceptions import NotFoundError
from services.agents import (
    BUILTIN,
    AgentCatalog,
    AgentDefinition,
    delegable,
    merged_roster,
    project_agents,
)
from services.projects import ProjectStore, WorktreeManager
from services.projects.tree import thread_tree
from services.workspace_kind import is_project_folder

from .deps import RunDeps

#: How many runs' catalogs are held at once, sized for the runs executing concurrently.
MAX_CATALOGS = 32

catalogs: OrderedDict[str, dict[str, AgentDefinition]] = OrderedDict()


async def run_roster(ctx: RunContext[RunDeps]) -> dict[str, AgentDefinition]:
    """Every agent this run may delegate to, by name."""
    return delegable(await run_catalog(ctx))


async def run_catalog(ctx: RunContext[RunDeps]) -> dict[str, AgentDefinition]:
    """Every published agent this run can see, by name."""
    run_id = ctx.deps.run.id
    cached = catalogs.pop(run_id, None)
    if cached is None:
        cached = await _resolve(ctx)
        if len(catalogs) >= MAX_CATALOGS:
            _ = catalogs.popitem(last=False)
    catalogs[run_id] = cached
    return cached


async def _resolve(ctx: RunContext[RunDeps]) -> dict[str, AgentDefinition]:
    root = await project_tree(ctx)
    catalog = ctx.deps.caps.get_optional(AgentCatalog)
    if catalog is None:
        return merged_roster(BUILTIN, project_agents(root) if root is not None else ())
    return await catalog.agents(ctx.deps.owner_id, project_root=root)


async def project_tree(ctx: RunContext[RunDeps]) -> Path | None:
    """The project tree this run works in, without resolving (and so holding) it."""
    deps = ctx.deps
    if not is_project_folder(deps.workspace_kind) or deps.project_id is None:
        return None
    if deps.workspace is not None:
        return deps.workspace.root
    projects = deps.caps.get_optional(ProjectStore)
    if projects is None:
        return None
    try:
        project = await projects.get(deps.owner_id, deps.project_id)
    except NotFoundError:
        return None
    root = Path(project.root_path)
    worktrees = deps.caps.get_optional(WorktreeManager)
    if worktrees is None:
        return root
    return await thread_tree(
        root, worktrees.path_for(deps.project_id), deps.workspace_kind, deps.conversation_id
    )
