"""A delegated agent's own checkout: cut from its parent's branch, merged back on ask.

A child worktree is a **fork of its parent**, not of the project. It branches from the
parent conversation's branch and is created out of the parent's *working tree state* —
the parent snapshots first, so the child opens on the files the parent's transcript
describes rather than on whatever was last committed. Anything else hands a delegated
agent a tree that contradicts the instructions it was given.

It sits beside the parent's checkout rather than inside it, and it is warm: the
parent's ``.venv`` and ``node_modules`` are cloned across (copy-on-write where the
filesystem allows), because a child that reinstalls them before it can run a test is a
child nobody delegates to.

**Merging back writes the parent's worktree, never the operator's tree.** That is the
whole point of the fence: the operator's own files are still only ever written by the
merge they press themselves. A conflict is reported, never resolved and never forced —
the merge aborts and touches neither tree, and what becomes of the child afterwards is
its caller's to decide (the delegation that asked for the fork throws it away).
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from core.fork import MergeReport, clone_tree

from .repo import AUTHOR, WorktreeError, child_branch_for, commit_worktree, run_git, run_git_ok

logger = logging.getLogger(__name__)

#: What is cloned from the parent so a child starts warm. Build outputs are not here on
#: purpose, and neither is ``.venv``: a virtualenv is bound to the absolute path it was
#: built at — shebangs, ``pyvenv.cfg``, ``VIRTUAL_ENV`` — so a cloned one would run the
#: parent's interpreter and install into the parent's environment from inside the child.
#: ``node_modules`` carries no such path and is the one that costs minutes to rebuild.
WARM_DIRS = ("node_modules",)


def child_path_for(
    worktrees_dir: Path, project_id: str, conversation_id: str, delegation_id: str
) -> Path:
    """Where a delegated child's checkout lives — beside the parent's, named after all
    three, so a leftover directory says which project, conversation and delegation owns
    it.

    The project is in the name because these share one directory with every project's
    checkout, and one conversation may work in several projects over its life: without
    it, a second delegation reusing an id would be handed the first project's tree while
    believing it was in the second.
    """
    return worktrees_dir / f"{project_id}-{conversation_id}-{delegation_id}"


async def add_child_worktree(
    *, root: Path, parent_path: Path, child: Path, branch: str, parent_branch: str
) -> None:
    """Check ``branch`` out in its own worktree, cut from the parent's branch, and warm
    it with the parent's dependency directories.

    Idempotent: delegating twice with the same id must not fail the second time, and a
    child worktree that already exists is the same tree this would have produced.
    """
    if (child / ".git").exists():
        return
    # A directory left behind by a previous run with no .git is not a worktree — clear
    # the registration and let the add below create it properly.
    _ = await run_git(root, "worktree", "prune")
    _ = await run_git_ok(root, "worktree", "add", "-b", branch, str(child), parent_branch)
    for name in WARM_DIRS:
        source = parent_path / name
        if source.is_dir() and not (child / name).exists():
            try:
                # Off the loop: a `node_modules` is gigabytes, and on a volume with no
                # clonefile support this is a real recursive copy — done inline it would
                # freeze every other conversation's stream for the length of it.
                await asyncio.to_thread(clone_tree, source, child / name)
            except OSError:
                # A cold child still works; it just pays the reinstall. Failing the
                # delegation over a cache would be the worse trade.
                logger.warning("fork: could not warm %s from %s", child / name, source)


async def merge_child_back(
    *, root: Path, parent_path: Path, child: Path, branch: str
) -> MergeReport:
    """Land the child's branch in the **parent's** worktree.

    Both trees are committed first, because nothing else commits: the child's work is
    uncommitted by construction (the agent has no ``git commit``), and a dirty parent is
    a merge git refuses outright — "your local changes would be overwritten" is not an
    answer the operator can act on.

    A conflict leaves everything where it is: the merge is aborted so the parent's tree
    stays usable, and the child is left exactly as it was for its caller to retire or
    keep. Only a clean landing retires it *here*.
    """
    _ = await commit_worktree(child, "Delegated agent changes")
    _ = await commit_worktree(parent_path, "Agent changes (before a delegated merge)")
    # Three dots: what the child added since it diverged, not what the parent has done
    # meanwhile — computed before the merge, while the two are still distinguishable.
    # Deletions are counted apart from the rest, because "your file changed" and "your
    # file is gone" are not the same sentence to the operator reading the report, and the
    # merge commit carries both.
    spec = f"HEAD...{branch}"
    files = _paths(await run_git_ok(parent_path, "diff", "--name-only", "--diff-filter=d", spec))
    deleted = _paths(await run_git_ok(parent_path, "diff", "--name-only", "--diff-filter=D", spec))

    code, out, err = await run_git(
        parent_path, *AUTHOR, "merge", "--no-ff", branch, "-m", f"Merged {branch}"
    )
    if code != 0:
        # Read the conflicting paths *before* aborting — the abort is what clears them.
        _code, unmerged, _err = await run_git(parent_path, "diff", "--name-only", "--diff-filter=U")
        conflicts = _paths(unmerged)
        _ = await run_git(parent_path, "merge", "--abort")
        if not conflicts:
            # Not a conflict at all — a broken ref, an unreadable tree. Say what git
            # said rather than report an empty, mystifying "nothing merged".
            raise WorktreeError((err or out).strip() or "the merge failed")
        return MergeReport(merged=False, files=[], conflicts=conflicts)

    await remove_child_worktree(root=root, child=child, branch=branch)
    return MergeReport(merged=True, files=files, deleted=deleted)


async def remove_child_worktree(*, root: Path, child: Path, branch: str) -> None:
    """Retire one delegated checkout — its worktree, then the branch it was on.

    Best-effort and idempotent, because the callers are a clean merge (which retires the
    child it just landed), a discarded conversation, and the delegation's own ``finally``
    — and that last one must not fail over a checkout one of the others already took.
    """
    _ = await run_git(root, "worktree", "remove", "--force", str(child))
    _ = await run_git(root, "branch", "-D", branch)


async def discard_children(
    *, root: Path, worktrees_dir: Path, project_id: str, conversation_id: str
) -> None:
    """Remove every delegated checkout this conversation left behind, and its branch.

    Best-effort, like the discard it is part of. Nothing else names these: a child is
    named after a delegation id that lives only in the run that asked for the fork, so a
    thread deleted mid-delegation would otherwise leave the delegated agent's files in
    the clear, a branch in the operator's repository, and a registration git keeps
    forever — and the next fork with that id would be silently handed the stale tree.
    """
    prefix = child_branch_for(conversation_id, "")
    code, out, _err = await run_git(
        root, "for-each-ref", "--format=%(refname:short)", f"refs/heads/{prefix}*"
    )
    if code != 0:
        return
    for branch in _paths(out):
        child = child_path_for(worktrees_dir, project_id, conversation_id, branch[len(prefix) :])
        await remove_child_worktree(root=root, child=child, branch=branch)
    # A child whose directory the operator had already deleted is removed by neither of
    # those; without the prune its registration outlives the branch it belonged to.
    _ = await run_git(root, "worktree", "prune")


def _paths(out: str) -> list[str]:
    return [line for line in out.splitlines() if line.strip()]
