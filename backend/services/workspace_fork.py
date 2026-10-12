"""Landing a delegated agent's forked workspace, and reporting what came back.

A sub-agent that works apart never edits the workspace that launched it. It gets its own
copy — a sandbox session cloned from the parent's, or its own checkout on its own branch —
and what it changed is merged back afterwards, with any conflict *reported* rather than
resolved in its favour.

**Taking the copy is not here**, and that is the shape rather than an omission. A
delegated run resolves its workspace the way every run does — repeatedly, from its own
workspace key, through ``services/workspace.py`` — so the fork is cut on its first
file-tool call by whichever manager owns that kind. What has no other home is the *other*
end: landing the copy happens when the child's run reaches terminal, from a hook with no
``RunContext``, no deps and no workspace left to resolve. So the parameters here are spelled
out rather than read off ``RunDeps``: ``services`` sits below ``tools`` in the dependency
order and must not import it.

Every handle is resolved optionally, which is the same degrade every capability-backed
feature makes: a host with no platform VM can still run a sub-agent, it just cannot
give one a workspace of its own, and the caller turns that into a sentence rather than an
exception.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from core.container import ServiceContainer
from core.fork import MergeReport
from services.projects.store import ProjectStore
from services.projects.worktree import WorktreeManager
from services.sandbox import SandboxSessionManager

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ForkTarget:
    """Where a fork is cut from, and the names the two halves are addressed by.

    ``delegation_id`` names the child for the whole of its life: a worktree fork is merged
    and discarded by (parent conversation, delegation id), so the caller that takes the
    fork and the terminal hook that lands it must agree on this one string. It is stored,
    not re-derived — an id that lived only in the call that asked for the fork is exactly
    how a checkout gets stranded.
    """

    #: The parent's workspace key (its conversation, normally), and the child's own.
    parent_key: str
    child_key: str
    delegation_id: str
    owner_id: str
    project_id: str | None = None
    #: The *parent's* conversation — what the worktree manager keys a child checkout on.
    conversation_id: str | None = None


@dataclass
class OpenFork:
    """An existing fork's two endings, for a caller that did not take it.

    Separate from :class:`Fork` because it answers a different question at a different
    time. Taking a copy needs a resolved workspace to copy *from*; landing one needs only
    the names, and happens long after the run that took it is gone — from a terminal hook
    with no ``RunContext``, no deps and no workspace left to resolve. Insisting on a
    workspace there would mean re-resolving one just to throw it away, which is how a
    merge ends up cutting a second fork.
    """

    merge: Callable[[], Awaitable[MergeReport]]
    discard: Callable[[], Awaitable[None]]


def reopen_fork(
    caps: ServiceContainer,
    *,
    kind: str,
    target: ForkTarget,
) -> OpenFork | None:
    """Handles for landing a fork somebody else took, or None when its manager is absent.

    ``kind`` is the workspace kind the fork was taken in — stored when it was taken, not
    guessed from the mode here, because this runs after everything that knew is gone.
    """
    if kind == "worktree":
        projects = caps.get_optional(ProjectStore)
        worktrees = caps.get_optional(WorktreeManager)
        if projects is None or worktrees is None or not target.project_id:
            return None

        async def where() -> dict:
            project = await projects.get(target.owner_id, target.project_id or "")
            return {
                "project_id": target.project_id,
                "root": Path(project.root_path),
                "conversation_id": target.conversation_id,
                "delegation_id": target.delegation_id,
            }

        async def merge_worktree() -> MergeReport:
            return await worktrees.merge_back(**await where())

        async def discard_worktree() -> None:
            await worktrees.discard_child(**await where())

        return OpenFork(merge=merge_worktree, discard=discard_worktree)

    sessions = caps.get_optional(SandboxSessionManager)
    if sessions is None:
        return None
    return OpenFork(
        merge=lambda: sessions.merge_back(target.child_key, target.parent_key),
        discard=lambda: sessions.purge(target.child_key),
    )


def merge_summary(report: MergeReport) -> str:
    """What came back, in the words the agent reading it needs to act on it.

    Conflicts and deletions are named rather than counted: "three files conflicted" is
    not something the agent reading this can do anything with, and the whole point of
    refusing to overwrite is that somebody decides afterwards which version is right.

    What it does *not* say is "compare the two versions". The fork goes away with the
    delegation either way, so the sub-agent's side of a conflict survives as its report
    and nowhere else, and pointing the parent at a copy that is gone costs it a turn
    discovering that.
    """
    lines = [
        "Merged back into your workspace: "
        + (", ".join(report.files) if report.files else "nothing.")
    ]
    if report.conflicts:
        lines.append(
            "Not merged — you changed these while the sub-agent ran, so your versions "
            "stand: "
            + ", ".join(report.conflicts)
            + ". Its copy is gone with the fork; if you still want its change, make it "
            "yourself from what it reported above."
        )
    if report.deleted:
        # Named on their own line rather than folded into the merged files: "your file
        # changed" and "your file is gone" are not the same sentence, and what a deletion
        # did to the parent differs by workspace kind (`core/fork.py`).
        lines.append("Deleted by the sub-agent: " + ", ".join(report.deleted))
    return "\n".join(lines)
