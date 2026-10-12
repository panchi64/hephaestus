"""The `repo` category — what a codebase already says about how to work in it.

`pydantic_ai_harness`'s `RepoContext`, split across the two seams this codebase has for
model-facing text:

- **the instructions half** — a project's own `CLAUDE.md` / `AGENTS.md`, loaded from the
  worktree root and handed to the model as standing instructions. Registered as an
  `InstructionProvider` rather than through the capability's `get_instructions`, because
  the capability is constructed once at app assembly and the workspace is per-run; a
  provider re-resolves each turn and can therefore read the run's own worktree. The
  content is static per project, so it stays cache-stable at the prompt head even though
  it is resolved dynamically — and it is **memoised for the run** rather than re-read,
  because a provider resolves on every model request and a turn makes up to
  `agent_request_limit` of them: the file walk, the SHA256 dedup and the budget's UTF-8
  encodes would otherwise run twenty-five times over an unchanged worktree, and an agent
  that edited the project's own `CLAUDE.md` mid-turn would rewrite the prompt head under
  itself and invalidate the whole turn's prefix cache. The loading itself is ours rather
  than the capability's (`repo_instructions.py`) for one reason: it has to be *budgeted*,
  and the capability reads whatever is on disk.
- **the inventory tool** — reports where the repo keeps its coding-assistant assets
  (`.claude/`, `.agents/`, `.codex/` and their `skills/`, `agents/`, `settings.json`). It
  locates them; reading them is the file tools' job. Rebound per run like every other
  workspace-rooted category.

Both are no-ops outside a project folder: a sandbox workspace is scratch space, and there
is no repository in it to have an opinion.
"""

from __future__ import annotations

from collections import OrderedDict
from pathlib import Path

from pydantic_ai import AbstractToolset, RunContext
from pydantic_ai_harness import RepoContext

from services.projects.worktree import WorktreeBusyError
from services.workspace_kind import is_project_folder

from .deps import RunDeps
from .rebound import WorkspaceToolset
from .repo_instructions import repo_instruction_text
from .workspace import run_workspace

#: The tool's own name inside the toolset; namespaced to `repo_inventory_agent_context`.
INVENTORY_TOOL = "inventory_agent_context"

#: How many runs' briefs are held at once, most-recently-used last. Sized for the runs
#: executing *concurrently* rather than for history — the host ceiling is eight lanes'
#: worth — with room to spare, since an entry costs at most the instruction budget and is
#: evicted long before a long-lived process has accumulated one per thread ever opened.
_MAX_BRIEFS = 32

_briefs: OrderedDict[tuple[str, str], str] = OrderedDict()


_CAPABILITY = RepoContext[RunDeps](
    # No walk-up: `home_dir=None` scans the worktree only. Climbing above it would
    # read instruction files from directories the operator never put in a project —
    # ultimately their home directory — which is not something to do quietly.
    home_dir=None,
    # Off: it hooks the file tools' results to surface a nested directory's
    # instruction file, and those results ride into context untrimmed here.
    nested_traversal=False,
    # Off because it is *ours*: the capability would load the instruction files whole,
    # and the prompt head is the one place a file of unbounded size must not land.
    # `repo_instructions.py` does the same load under a byte budget; what remains of
    # the capability's own brief is the inventory tool's one-line hint.
    autoload_instructions=False,
    expose_inventory_tool=True,
)


async def repo_instructions(ctx: RunContext[RunDeps]) -> str:
    """The project's own instruction files, as standing instructions for the run."""
    if not is_project_folder(ctx.deps.workspace_kind):
        return ""
    try:
        workspace = await run_workspace(ctx)
    except WorktreeBusyError:
        return ""
    if workspace is None or workspace.kind != "worktree":
        return ""
    return await _run_brief(ctx.deps.run.id, workspace.root)


async def _run_brief(run_id: str, root: Path) -> str:
    """This run's brief for this worktree, built once and reused for every later request
    the run makes. Keyed by the run rather than by the root so a later turn — the one
    place a project's instruction file may legitimately have changed since it was read —
    still re-reads it, and so a resumed approval keeps the head its earlier requests were
    cached against. An empty brief is a cached answer like any other."""
    key = (run_id, str(root))
    brief = _briefs.pop(key, None)
    if brief is None:
        brief = await _brief(root)
        if len(_briefs) >= _MAX_BRIEFS:
            _briefs.popitem(last=False)
    _briefs[key] = brief
    return brief


async def _brief(root: Path) -> str:
    """The budgeted instruction files, then the capability's own inventory hint — the
    same order and the same joiner its `get_instructions` uses, so moving the loading out
    from under it changed what is *bounded*, not what the model reads.

    The hint names the tool by the harness's own un-namespaced function name, and this
    catalog offers it as `repo_inventory_agent_context`; a brief pointing at a tool that
    is not on offer is worse than one pointing at nothing. `describe.py` fixes the same
    mismatch inside tool *descriptions* — instructions are the other seam, and they do not
    pass through it."""
    hint = str(_CAPABILITY.get_instructions() or "").replace(
        INVENTORY_TOOL, f"repo_{INVENTORY_TOOL}"
    )
    parts = (await repo_instruction_text(root), hint)
    return "\n\n".join(part for part in parts if part)


def repo_toolset() -> AbstractToolset[RunDeps]:
    """The `repo` category, built once at app assembly and shared by every run."""
    toolset = _CAPABILITY.get_toolset()
    assert toolset is not None  # `expose_inventory_tool` is on
    return WorkspaceToolset("repo", toolset)
