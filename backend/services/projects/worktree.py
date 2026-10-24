"""Git worktrees: where a chat works when the agent wants its changes kept apart.

A worktree conversation gets a **branch** (``heph/<conversation-id>``) checked out in a
worktree beside the repository. The agent edits and runs tests there, and only an explicit
merge the operator asks for touches their own tree.

**The chassis commits, because nothing else does.** The agent has no `git commit`; when
work becomes a commit is not the model's call. `snapshot` stages and commits the worktree
onto the conversation's branch, and `diff`/`merge` call it before they read. Without it a
ref-to-ref diff reports nothing after a session that rewrote ten files, MERGE lands
nothing, and the delete gate never fires. History is one commit per review, not per turn.

**One worktree per project, not per conversation.** A worktree is a full checkout, so
per-conversation would reinstall ``node_modules`` / ``.venv`` / build caches for every
thread. The cost is that one coding conversation holds a project at a time, which is
enforced. A delegated child of the holder is the exception: it gets its own checkout
beside the parent's, owned by the same conversation (:mod:`services.projects.fork`).

**Worktrees live outside ``data_dir``.** Approved host commands are fenced by
``services/sandbox/fence.py``, which denies reads of the whole data directory, so a
worktree beneath it would be unreadable by the shell that builds it. A worktree is
plaintext on disk, a checkout of the operator's already-plaintext repository.

**``git init`` is never implicit.** `ensure_repo` runs it only on the operator's explicit
confirmation, asked by the Projects UI at project creation, never by the agent mid-turn.

**A merge releases the project**, clearing the holder map entry, as `discard` does. Merging
a fork back does not: that is a delegated agent's work rejoining a parent still working.

A worktree branches from the project's base ref, so **uncommitted work in the operator's
own checkout is invisible to the agent**; `RepoProbe.uncommitted_changes` lets the UI say
so.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path

from core.exceptions import InvalidInputError
from core.fork import MergeReport

from .diffstat import FileChange, file_changes
from .fork import (
    add_checkout_child,
    add_child_worktree,
    apply_child,
    child_changes,
    child_path_for,
    discard_children,
    merge_child_back,
    remove_child_worktree,
)
from .repo import (
    AUTHOR,
    WorktreeError,
    WorktreeState,
    branch_for,
    child_branch_for,
    commit_worktree,
    run_git,
    run_git_ok,
)

logger = logging.getLogger(__name__)


class WorktreeBusyError(Exception):
    """Another worktree conversation is holding this project's worktree.

    Refused rather than queued or shared: two threads interleaving edits over one checkout
    would corrupt both their views of the tree.
    """


@dataclass(frozen=True)
class Diff:
    """What a worktree conversation has changed, against the project's base ref.

    The merge gate asks three things at once: the raw patch, the per-file rows with their
    risk verdict (:mod:`services.projects.diffstat`), and how the branch stands against the
    base (whether the review is still current).
    """

    branch: str
    files_changed: int
    insertions: int
    deletions: int
    patch: str
    #: Per-file rows, already ordered by how much the operator should look at each.
    files: list[FileChange] = field(default_factory=list)
    #: Commits on the branch the base does not have.
    ahead: int = 0
    #: Commits the base has that the branch does not: the staleness number.
    behind: int = 0
    #: The branch tip's committer date, ISO 8601 with offset, or None before the branch has
    #: commits of its own. The other half of staleness: how long ago, not just how far.
    last_commit_at: str | None = None


class WorktreeManager:
    """Owns each project's single worktree and which conversation holds it."""

    def __init__(self, worktrees_dir: Path) -> None:
        self._root: Path = worktrees_dir
        # project_id -> conversation_id currently holding the checkout.
        self._holders: dict[str, str] = {}
        self._lock: asyncio.Lock = asyncio.Lock()

    def path_for(self, project_id: str) -> Path:
        return self._root / project_id

    def fork_path(self, project_id: str, conversation_id: str, delegation_id: str) -> Path:
        """Where a delegated child of this conversation checks out."""
        return child_path_for(self._root, project_id, conversation_id, delegation_id)

    def holder(self, project_id: str) -> str | None:
        return self._holders.get(project_id)

    def release(self, project_id: str, conversation_id: str) -> None:
        """Give up the checkout if this conversation holds it. Idempotent."""
        if self._holders.get(project_id) == conversation_id:
            del self._holders[project_id]

    async def is_repo(self, root: Path) -> bool:
        code, _, _ = await run_git(root, "rev-parse", "--is-inside-work-tree")
        return code == 0

    async def ensure_repo(self, root: Path, *, confirmed: bool) -> bool:
        """Make ``root`` a git repository, returning whether we created one.

        ``confirmed`` is the operator's explicit yes; without it this refuses, since
        committing a whole directory into a new repository must not be a surprise.
        """
        if await self.is_repo(root):
            return False
        if not confirmed:
            raise InvalidInputError(
                f"{root} is not a git repository. A worktree needs one so your work can "
                + "be kept separate — confirm to create it."
            )
        _ = await run_git_ok(root, "init")
        _ = await run_git_ok(root, "add", "-A")
        # An empty directory still needs the base commit a worktree branches from.
        _ = await run_git_ok(root, *AUTHOR, "commit", "--allow-empty", "-m", "Initial commit")
        logger.info("projects: initialised a git repository at %s", root)
        return True

    async def acquire(
        self, *, project_id: str, root: Path, base_ref: str, conversation_id: str
    ) -> WorktreeState:
        """Check this conversation's branch out in the project's worktree.

        Idempotent for the conversation already holding it; refuses while another one
        does. Creating the branch and the worktree are both idempotent, so a restart or
        a second turn in the same thread simply re-acquires.
        """
        async with self._lock:
            self._require_free(project_id, conversation_id)

            if not await self.is_repo(root):
                raise InvalidInputError(
                    f"{root} is not a git repository — open the project's page to set one up."
                )

            branch = branch_for(conversation_id)
            path = self.path_for(project_id)
            path.parent.mkdir(parents=True, exist_ok=True)

            # Branch first, so `worktree add` never has to create it.
            code, _, _ = await run_git(root, "rev-parse", "--verify", branch)
            if code != 0:
                _ = await run_git_ok(root, "branch", branch, base_ref)

            if (path / ".git").exists():
                # Commit the previous holder's leftovers before switching: `git checkout`
                # carries uncommitted files across, so one thread's half-finished work
                # would land on another's branch, or the checkout would fail.
                _ = await commit_worktree(path, "Agent changes (carried forward)")
                _ = await run_git_ok(path, "checkout", branch)
            else:
                # A leftover directory with no .git is not a worktree: prune and re-add.
                _ = await run_git(root, "worktree", "prune")
                _ = await run_git_ok(root, "worktree", "add", str(path), branch)

            self._holders[project_id] = conversation_id
            return WorktreeState(path=path, branch=branch, base_ref=base_ref)

    def _require_free(self, project_id: str, conversation_id: str) -> None:
        """Refuse when another conversation is working in this project; called under the
        lock. A conversation's own delegated children pass."""
        holder = self._holders.get(project_id)
        if holder is not None and holder != conversation_id:
            raise WorktreeBusyError(
                "Another worktree conversation is currently working in this project. "
                + "Finish or merge that one first."
            )

    async def cut_child(
        self,
        *,
        project_id: str,
        root: Path,
        conversation_id: str,
        delegation_id: str,
        source: WorktreeState | None,
    ) -> WorktreeState:
        """A sub-agent's own worktree, cut from ``source`` (a worktree its parent works
        in, committed first) or, with no source, from the checkout as it stands now.

        ``conversation_id`` is the thread whose worktrees the child's is named under. The
        returned ``base_ref`` is the commit the child starts at, which is what a later
        merge diffs against.
        """
        async with self._lock:
            branch = child_branch_for(conversation_id, delegation_id)
            child = self.fork_path(project_id, conversation_id, delegation_id)
            child.parent.mkdir(parents=True, exist_ok=True)
            if source is None:
                if not await self.is_repo(root):
                    raise WorktreeError(f"{root} is not a git repository")
                await add_checkout_child(root=root, child=child, branch=branch)
            else:
                if not (source.path / ".git").exists():
                    raise WorktreeError("the worktree to cut from is not open")
                _ = await commit_worktree(source.path, "Agent changes (forked from)")
                await add_child_worktree(
                    root=root,
                    parent_path=source.path,
                    child=child,
                    branch=branch,
                    parent_branch=source.branch,
                )
            base = (await run_git_ok(child, "rev-parse", "HEAD")).strip()
            return WorktreeState(path=child, branch=branch, base_ref=base)

    def reopen_child(
        self, *, project_id: str, conversation_id: str, delegation_id: str
    ) -> WorktreeState:
        """A sub-agent's worktree as it stands, read off the disk so a restart changes
        nothing. Raises ``WorktreeError`` once it has been merged or discarded."""
        child = self.fork_path(project_id, conversation_id, delegation_id)
        if not (child / ".git").exists():
            raise WorktreeError("this agent's worktree was merged or discarded")
        return WorktreeState(
            path=child,
            branch=child_branch_for(conversation_id, delegation_id),
            base_ref=branch_for(conversation_id),
        )

    async def merge_child(
        self,
        *,
        project_id: str,
        root: Path,
        conversation_id: str,
        delegation_id: str,
        into: Path,
    ) -> MergeReport:
        """Merge a sub-agent's branch into the worktree at ``into`` and retire it; a
        conflict aborts and keeps it (:func:`services.projects.fork.merge_child_back`)."""
        async with self._lock:
            child = self.reopen_child(
                project_id=project_id,
                conversation_id=conversation_id,
                delegation_id=delegation_id,
            )
            return await merge_child_back(
                root=root, parent_path=into, child=child.path, branch=child.branch
            )

    async def apply_child(
        self,
        *,
        project_id: str,
        root: Path,
        conversation_id: str,
        delegation_id: str,
        base_ref: str,
    ) -> MergeReport:
        """Apply a sub-agent's changes since ``base_ref`` to the checkout at ``root``, then
        retire it; a clash keeps it (:func:`services.projects.fork.apply_child`)."""
        async with self._lock:
            child = self.reopen_child(
                project_id=project_id,
                conversation_id=conversation_id,
                delegation_id=delegation_id,
            )
            report = await apply_child(checkout=root, child=child.path, base_ref=base_ref)
            if report.merged:
                await remove_child_worktree(root=root, child=child.path, branch=child.branch)
            return report

    async def child_changes(
        self, *, project_id: str, conversation_id: str, delegation_id: str, base_ref: str
    ) -> MergeReport:
        """What a sub-agent's worktree changed since ``base_ref``, committed first."""
        async with self._lock:
            child = self.reopen_child(
                project_id=project_id,
                conversation_id=conversation_id,
                delegation_id=delegation_id,
            )
            return await child_changes(child=child.path, base_ref=base_ref)

    async def discard_child(
        self, *, project_id: str, root: Path, conversation_id: str, delegation_id: str
    ) -> None:
        """Throw one sub-agent's worktree and branch away, whatever became of its work.
        Idempotent, since a clean merge may already have retired it."""
        async with self._lock:
            await remove_child_worktree(
                root=root,
                child=self.fork_path(project_id, conversation_id, delegation_id),
                branch=child_branch_for(conversation_id, delegation_id),
            )

    async def branch_from(self, root: Path, *, source_id: str, conversation_id: str) -> None:
        """Cut this conversation's branch from another conversation's, for a fork.

        The fork inherits a transcript describing the source's files, so branching from the
        base ref would contradict its history. Raises when the source has no branch; the
        caller then lets the fork cut its own from the base ref on its first coding turn.
        """
        _ = await run_git_ok(root, "rev-parse", "--verify", branch_for(source_id))
        target = branch_for(conversation_id)
        code, _, _ = await run_git(root, "rev-parse", "--verify", target)
        if code == 0:
            return  # forking twice must not fail
        _ = await run_git_ok(root, "branch", target, branch_for(source_id))

    async def snapshot(self, project_id: str, *, conversation_id: str) -> bool:
        """Commit whatever the agent has changed in the worktree onto its branch.

        Nothing else commits (see the module docstring); this runs right before anything
        reads the branch. Returns whether it committed. Only the conversation holding the
        checkout may snapshot, or another thread's leftovers would be attributed to this
        branch.
        """
        if self._holders.get(project_id) != conversation_id:
            return False
        return await commit_worktree(self.path_for(project_id))

    async def diff(
        self, root: Path, *, base_ref: str, conversation_id: str, project_id: str
    ) -> Diff:
        """What this conversation changed: the patch, the per-file rows, and how the
        branch stands against the base.

        Snapshots first so the review includes the agent's latest work. Counts come from git
        rather than the possibly megabyte patch; ``-M`` makes a moved file read as a move,
        not a whole-file delete plus add.
        """
        _ = await self.snapshot(project_id, conversation_id=conversation_id)
        branch = branch_for(conversation_id)
        # Three dots: the branch's changes since divergence, not the operator's own.
        spec = f"{base_ref}...{branch}"
        patch = await run_git_ok(root, "diff", spec)
        stat = await run_git_ok(root, "diff", "--shortstat", spec)
        numstat = await run_git_ok(root, "diff", "--numstat", "-M", "-z", spec)
        name_status = await run_git_ok(root, "diff", "--name-status", "-M", "-z", spec)
        ahead, behind = await self.divergence(root, base_ref=base_ref, branch=branch)
        return Diff(
            branch=branch,
            files_changed=_stat_field(stat, "file"),
            insertions=_stat_field(stat, "insertion"),
            deletions=_stat_field(stat, "deletion"),
            patch=patch,
            files=file_changes(numstat, name_status),
            ahead=ahead,
            behind=behind,
            last_commit_at=await self._last_commit_at(root, branch),
        )

    async def divergence(self, root: Path, *, base_ref: str, branch: str) -> tuple[int, int]:
        """(ahead, behind): commits the branch has that the base lacks, and the reverse.

        ``rev-list --left-right --count base...branch`` prints ``behind ahead``. Best-effort:
        a deleted or renamed base stops staleness reporting without failing the diff read.
        """
        code, out, _ = await run_git(
            root, "rev-list", "--left-right", "--count", f"{base_ref}...{branch}"
        )
        if code != 0:
            return 0, 0
        parts = out.split()
        if len(parts) != 2 or not all(part.isdigit() for part in parts):
            return 0, 0
        return int(parts[1]), int(parts[0])

    async def _last_commit_at(self, root: Path, branch: str) -> str | None:
        """The branch tip's committer date, ISO 8601 with offset; None before any commit."""
        code, out, _ = await run_git(root, "log", "-1", "--format=%cI", branch)
        return out.strip() or None if code == 0 else None

    async def merge(
        self, root: Path, *, base_ref: str, conversation_id: str, project_id: str
    ) -> str:
        """Land the branch on the base ref: the one operation that writes the operator's own
        tree, which is why they press it themselves.

        **Refuses when their checkout is not on the base ref.** `git merge` lands on HEAD,
        but the reviewed diff was computed against `base_ref`.

        Snapshots first, as `diff` does, then releases the project for the next worktree
        conversation.
        """
        _ = await self.snapshot(project_id, conversation_id=conversation_id)
        await self._require_base_checked_out(root, base_ref)
        branch = branch_for(conversation_id)
        out = await run_git_ok(root, *AUTHOR, "merge", "--no-ff", branch, "-m", f"Merged {branch}")
        self.release(project_id, conversation_id)
        return out

    async def _require_base_checked_out(self, root: Path, base_ref: str) -> None:
        """Refuse a merge onto a ref the operator is not standing on.

        A base of `HEAD` (no branch name learned) always matches. A detached HEAD reports
        the literal `HEAD` from `--abbrev-ref`, which does not match a named base; refusing
        is right, since a merge into a detached HEAD is lost work.
        """
        if base_ref == "HEAD":
            return
        code, current, _ = await run_git(root, "rev-parse", "--abbrev-ref", "HEAD")
        if code != 0:
            return  # not a state we can read; let the merge itself report the problem
        current = current.strip()
        if current != base_ref:
            raise WorktreeError(
                f"Your working tree is on {current!r}, but this conversation's changes "
                + f"were reviewed against {base_ref!r}. Check out {base_ref!r} first, or "
                + "change the project's base ref — merging here would land a different "
                + "result from the diff you just read."
            )

    async def discard(self, root: Path, *, project_id: str, conversation_id: str) -> None:
        """Throw the branch away, and every delegated checkout cut from it. Best-effort and
        idempotent, since a half-set-up thread must not block conversation deletion."""
        await discard_children(
            root=root,
            worktrees_dir=self._root,
            project_id=project_id,
            conversation_id=conversation_id,
        )
        branch = branch_for(conversation_id)
        path = self.path_for(project_id)
        if self._holders.get(project_id) == conversation_id:
            # Uncommitted files would otherwise be carried onto the next branch taken up.
            _ = await run_git(path, "reset", "--hard")
            _ = await run_git(path, "clean", "-fd")
            # Park off the branch so it can be deleted. `--detach` rather than the base
            # ref, which is normally checked out in the main tree (git refuses twice).
            _ = await run_git(path, "checkout", "--detach")
            del self._holders[project_id]
        _ = await run_git(root, "branch", "-D", branch)

    async def discard_children(self, root: Path, *, project_id: str, conversation_id: str) -> None:
        """Throw away every sub-agent worktree named under this conversation, for a thread
        deleted with no worktree of its own. Best-effort, like :meth:`discard`."""
        async with self._lock:
            await discard_children(
                root=root,
                worktrees_dir=self._root,
                project_id=project_id,
                conversation_id=conversation_id,
            )


def _stat_field(shortstat: str, word: str) -> int:
    """One number out of `git diff --shortstat`; 0 for an empty diff's empty string."""
    for part in shortstat.split(","):
        if word in part:
            digits = "".join(ch for ch in part if ch.isdigit())
            return int(digits) if digits else 0
    return 0
