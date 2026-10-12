"""`run_workspace(ctx)` — the workspace resolver, as a tool sees it.

`services/workspace.py` holds the resolution; this is the half that reads it off
`RunDeps` and the capability bag, so a tool asks one question and gets one answer rather
than each reaching for `SandboxSessionManager` and branching on the workspace kind itself. Two
spellings, one answer: tools hold a `RunContext`, and the approval gate — which has to
resolve the workspace *before* any tool has run, to judge a command's paths against the
directory it will actually run in — holds only the run's deps.

The answer is memoised **on the run's own deps**, not in a module-level cache: a code
turn's first call does real work (`git worktree add`, a branch checkout) and a turn makes
many file-tool calls, but the memo must die with the run rather than outlive it in a
global. Only a successful resolution is kept — a sandbox that failed to open may open on
the next call, and the failure path costs nothing to retry.
"""

from __future__ import annotations

from pydantic_ai import RunContext

from services.projects.checkout import CheckoutHolders
from services.projects.store import ProjectStore
from services.projects.worktree import WorktreeManager
from services.sandbox import SandboxSessionManager
from services.workspace import RunWorkspace, resolve_workspace
from services.workspace_kind import is_project_folder

from .deps import RunDeps

# The words the model gets when there is nowhere to work, in either workspace. Kept identical
# to `code_execute`'s so a missing runtime reads as one fact about one machine.
NO_WORKSPACE = (
    "Your computer is unavailable right now: no runtime is configured. Files cannot "
    "be read or written, and nothing will run on the operator's host."
)

NO_PROJECT = (
    "This conversation has no project workspace: its project is missing or its "
    "folder or git worktree could not be opened. Ask the operator to check the project."
)


async def run_workspace(ctx: RunContext[RunDeps]) -> RunWorkspace | None:
    """Where this run's file work happens, or None when it has nowhere to work.

    The tool-facing spelling of :func:`resolve_run_workspace` — a tool holds a
    ``RunContext``, and unwrapping it at each call site would be one more thing to get
    right in twenty places.
    """
    return await resolve_run_workspace(ctx.deps)


async def resolve_run_workspace(deps: RunDeps) -> RunWorkspace | None:
    """The same answer, for a caller that has the run's deps but no ``RunContext``.

    The approval gate is that caller (``agent/gating.py``): it judges a deferred shell
    command *before* any tool has run, so reading the memo alone would find it empty and
    measure the first command of a turn against no workspace at all. It has to be able to
    resolve one, and it has to get the identical answer the tool will — the memo below is
    what guarantees the second half.

    None is the degrade signal every caller already knows how to handle: a sandbox with
    no runtime, a worktree with no project bound. A busy worktree raises
    instead — the operator has to be told, not quietly given a different filesystem.
    """
    if deps.workspace is not None:
        return deps.workspace

    workspace = await resolve_workspace(
        workspace_kind=deps.workspace_kind,
        project_id=deps.project_id,
        conversation_id=deps.conversation_id,
        workspace_key=deps.workspace_key,
        owner_id=deps.owner_id,
        sessions=deps.caps.get_optional(SandboxSessionManager),
        projects=deps.caps.get_optional(ProjectStore),
        worktrees=deps.caps.get_optional(WorktreeManager),
        checkouts=deps.caps.get_optional(CheckoutHolders),
        # The run claims its session for as long as it lasts, not for the length of
        # one tool call: the live-session cap would otherwise be free to reap the guest copy
        # between two of them, dropping the live processes and system state around an
        # install or a clone.
        holder=deps.run,
    )
    deps.workspace = workspace
    return workspace


def unavailable(deps: RunDeps) -> str:
    """The right "no workspace" sentence for the kind of workspace the run is in."""
    return NO_PROJECT if is_project_folder(deps.workspace_kind) else NO_WORKSPACE
