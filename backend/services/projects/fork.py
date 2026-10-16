"""A sub-agent's own worktree: cut from where its parent works, landed when the parent
says so.

A child worktree is cut from its parent's *working state*, not its last commit: from the
parent's worktree, committed first, or from the operator's checkout with its uncommitted
files carried across. Either way the child opens on the files the parent's instructions
describe. It is warm: ``node_modules`` is cloned across (copy-on-write where the
filesystem allows), because a child that reinstalls before it can run a test is a child
nobody delegates to.

Landing it is the parent's call. Into a parent worktree it is a git merge; into the
checkout it is the child's own changes applied as plain file changes, never a commit or
an index write. A conflict is reported and touches neither tree, and the child is kept.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
import tempfile
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
    await _warm(parent_path, child)


async def add_checkout_child(*, root: Path, child: Path, branch: str) -> None:
    """Check ``branch`` out at the checkout's ``HEAD`` in its own worktree, with the
    checkout's uncommitted and untracked files carried across and committed as its start.

    The child opens on the files the operator has now, not on their last commit, and the
    starting commit keeps those files out of what the child is later merged with.
    """
    if (child / ".git").exists():
        return
    _ = await run_git(root, "worktree", "prune")
    _ = await run_git_ok(root, "worktree", "add", "-b", branch, str(child), "HEAD")
    listed = await run_git_ok(root, "ls-files", "-z", "-m", "-o", "--exclude-standard")
    await asyncio.to_thread(_carry, root, child, [p for p in listed.split("\0") if p])
    _ = await commit_worktree(child, "Starting point: the checkout's uncommitted work")
    await _warm(root, child)


def _carry(root: Path, child: Path, paths: list[str]) -> None:
    for rel in paths:
        source, target = root / rel, child / rel
        if source.is_symlink() or source.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.unlink(missing_ok=True)
            _ = shutil.copy2(source, target, follow_symlinks=False)
        elif not source.exists():
            target.unlink(missing_ok=True)


async def _warm(source_root: Path, child: Path) -> None:
    for name in WARM_DIRS:
        source = source_root / name
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
    """Land the child's branch in the **parent's** worktree, and retire the child.

    Both trees are committed first: a dirty parent is a merge git refuses outright. A
    conflict aborts the merge, so the parent's tree stays usable and the child is kept.
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


async def child_changes(*, child: Path, base_ref: str) -> MergeReport:
    """What the child changed since it was cut, with its tree committed first."""
    _ = await commit_worktree(child, "Delegated agent changes")
    spec = f"{base_ref}..HEAD"
    files = _paths(
        await run_git_ok(child, "diff", "--no-renames", "--name-only", "--diff-filter=d", spec)
    )
    deleted = _paths(
        await run_git_ok(child, "diff", "--no-renames", "--name-only", "--diff-filter=D", spec)
    )
    return MergeReport(merged=False, files=files, deleted=deleted)


#: The path in each ``git apply --check`` complaint.
_APPLY_ERROR = re.compile(r"^error: (?:patch failed: )?(.+?):(?: |\d|$)", re.MULTILINE)


async def apply_child(*, checkout: Path, child: Path, base_ref: str) -> MergeReport:
    """Apply the child's changes since ``base_ref`` to the checkout's working tree.

    Only files are written, never the checkout's index or history, so the operator's own
    git state is untouched. A patch that does not apply cleanly is refused whole, naming
    the files that clashed, and the child is kept.
    """
    changed = await child_changes(child=child, base_ref=base_ref)
    if not changed.files and not changed.deleted:
        return MergeReport(merged=True, files=[])
    with tempfile.TemporaryDirectory() as scratch:
        # Written by git itself: a text file in another encoding must survive byte-exact.
        patch_file = Path(scratch) / "child.patch"
        _ = await run_git_ok(
            child, "diff", "--no-renames", "--binary", f"--output={patch_file}", f"{base_ref}..HEAD"
        )
        code, out, err = await run_git(checkout, "apply", "--check", str(patch_file))
        if code != 0:
            clashed = sorted(set(_APPLY_ERROR.findall(err or out)))
            if not clashed:
                raise WorktreeError((err or out).strip() or "the changes could not be applied")
            return MergeReport(merged=False, files=[], conflicts=clashed)
        _ = await run_git_ok(checkout, "apply", str(patch_file))
    return MergeReport(merged=True, files=changed.files, deleted=changed.deleted)


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
