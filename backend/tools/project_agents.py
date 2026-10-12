"""The sub-agent roster this run may launch from — built-ins plus the project's own.

A project declares sub-agents in files (``services/subagents/definitions.py``); which
project that is depends on the run, so the roster cannot be assembled once at startup
alongside the toolset. This is the same seam ``tools/repo.py`` uses for a project's
instruction files, for the same reasons and with the same memoisation:

**Resolved per run, cached per run.** The roster is read where the model is deciding —
inside ``get_tools``, which is called on *every* model request, up to the request limit for
a single turn. Re-walking ``.claude/agents`` and re-parsing every file twenty-five times
over an unchanged checkout would be pure waste; worse, an agent that edited one of those
files mid-turn would rewrite the launch tool's description under itself and invalidate the
whole turn's prefix cache. Keyed by the run rather than by the directory so the next turn
still re-reads — a later turn is exactly where a project's agent files may legitimately
have changed since.

**Project folders only.** A sandbox workspace is scratch space with no repository in it to
declare anything, so outside a worktree this is the built-ins and nothing else — and, more
to the point, no filesystem walk at all.
"""

from __future__ import annotations

from collections import OrderedDict
from pathlib import Path

from pydantic_ai import RunContext

from services.projects.worktree import WorktreeBusyError
from services.subagents import BUILTIN, SubagentSpec, merged_roster
from services.subagents.definitions import project_specs
from services.workspace_kind import is_project_folder

from .deps import RunDeps
from .workspace import run_workspace

#: How many runs' rosters are held at once, most-recently-used last. Sized for the runs
#: executing concurrently rather than for history, the same as the instruction briefs
#: beside it: an entry is a handful of small records, and it is evicted long before a
#: long-lived process has accumulated one per thread ever opened.
_MAX_ROSTERS = 32

_rosters: OrderedDict[tuple[str, str], tuple[SubagentSpec, ...]] = OrderedDict()


async def run_roster(ctx: RunContext[RunDeps]) -> dict[str, SubagentSpec]:
    """Every sub-agent this run may launch, by name.

    Built-ins first so a project shadows one by declaring its own of the same name — a
    repository that has written down how *its* reviewer works knows something this
    installation does not.
    """
    return merged_roster(BUILTIN, await _project_specs(ctx))


async def _project_specs(ctx: RunContext[RunDeps]) -> tuple[SubagentSpec, ...]:
    if not is_project_folder(ctx.deps.workspace_kind):
        return ()
    try:
        workspace = await run_workspace(ctx)
    except WorktreeBusyError:
        # Held by a sibling run. The built-ins are still launchable, and refusing the
        # whole roster over a lock would be a worse answer than a slightly smaller one.
        return ()
    if workspace is None or workspace.kind != "worktree":
        return ()
    return _cached(ctx.deps.run.id, workspace.root)


def _cached(run_id: str, root: Path) -> tuple[SubagentSpec, ...]:
    key = (run_id, str(root))
    specs = _rosters.pop(key, None)
    if specs is None:
        specs = project_specs(root)
        if len(_rosters) >= _MAX_ROSTERS:
            _rosters.popitem(last=False)
    _rosters[key] = specs
    return specs
