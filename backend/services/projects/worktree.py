"""Git worktrees — where a chat works when the agent wants its changes kept apart.

A worktree never writes the operator's checkout. A worktree conversation gets a **branch**
(``heph/<conversation-id>``) checked out in a worktree beside the repository, the agent
edits and runs tests there, and the only thing that ever touches the operator's own tree
is an explicit merge they ask for.

**The chassis commits, because nothing else does.** The agent edits with its file and
shell tools and has no `git commit` — deliberately: when a body of work becomes a commit
is not a judgement to hand the model. So `snapshot` stages and commits the worktree onto
the conversation's branch, and `diff`/`merge` call it before they read. Skip that and
every downstream step is inert *while looking correct*: a ref-to-ref diff reports nothing
after a session that rewrote ten files, MERGE lands nothing, and the delete gate never
fires. The history is one commit per review rather than one per turn — coarse, but honest
about what it is.

Four more decisions here are load-bearing and none of them is obvious from the code.

**One worktree per project, not per conversation.** A worktree is a full checkout, so
per-conversation would mean a fresh ``node_modules`` / ``.venv`` / build cache for every
thread — minutes of reinstall before the agent does anything useful, and no reap policy
short of deleting the thread. Sharing one per project keeps those caches warm, which is
most of the reason to code on the host at all. The cost is that only one coding
conversation can hold a project at a time; that is enforced rather than allowed to
silently interleave two threads over one checkout. A *delegated child* of the holding
conversation is the one exception — it gets its own checkout beside the parent's, still
owned by the same conversation (see :mod:`services.projects.fork`).

**Worktrees live outside ``data_dir``.** An approved host command is fenced by
``services/sandbox/host.py``, which denies reads of the whole data directory — the vault,
the sealed workspaces and the database are in there. A worktree beneath it would be
unreadable by the very shell that has to build and test it. The consequence is that a
worktree is plaintext on disk; it is a checkout of the operator's own already-plaintext
repository, so it exposes nothing that was not already exposed.

**``git init`` is never implicit.** Creating a repository in someone's directory is a
real, visible side effect. `ensure_repo` performs it, but only when the caller has an
explicit confirmation from the operator — the Projects UI asks at project-creation time,
not the agent mid-turn.

**A merge releases the project.** The holder map is what enforces one worktree thread per
checkout, and `discard` used to be the only thing that cleared it — so a merged thread
kept the project locked forever, and the busy message told the operator to do the very
thing they had just done. Merging hands the project back. Merging a *fork* back does
not: that is one delegated agent's work rejoining its parent, and the parent is still
working.

A last thing worth knowing because it will otherwise be discovered halfway through a
session: a worktree branches from the project's base ref, so **uncommitted work in the
operator's own checkout is invisible to the agent**. `RepoProbe.uncommitted_changes`
exists so the UI can say so up front.
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

    Refused rather than queued or silently shared: two threads interleaving edits over
    one checkout would corrupt both their mental models of the tree.
    """


@dataclass(frozen=True)
class Diff:
    """What a worktree conversation has changed, against the project's base ref.

    Three answers in one read, because the merge gate asks all three at once: the raw
    patch (what changed), the per-file rows with their risk verdict (what to look at
    first — see :mod:`services.projects.diffstat`), and how the branch stands against the
    base (whether what you are reading is still current).
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
    #: Commits the base has that the branch does not — the staleness number. A branch
    #: cut days ago from a base that has moved is reviewing work against a tree that no
    #: longer exists, and nothing on screen said so.
    behind: int = 0
    #: The branch tip's committer date, ISO 8601 with offset, or None for a branch with
    #: no commits of its own yet. The other half of staleness: how long ago, not just
    #: how far.
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

        ``confirmed`` is the operator's explicit yes. Without it this refuses rather
        than initialising: committing someone's entire directory into a new repository
        is not something to discover after the fact.
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
        # `--allow-empty` so an empty directory still gets the base commit a worktree
        # must branch from.
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

            # Branch first (idempotent): `worktree add` on an existing branch must not
            # try to create it again.
            code, _, _ = await run_git(root, "rev-parse", "--verify", branch)
            if code != 0:
                _ = await run_git_ok(root, "branch", branch, base_ref)

            if (path / ".git").exists():
                # Commit whatever the previous holder left behind **before** switching.
                # Nothing else commits (see `snapshot`), so the worktree is dirty far more
                # often than not — and `git checkout` carries uncommitted files across, so
                # without this one thread's half-finished work lands on another thread's
                # branch, or the checkout fails outright with a raw git error.
                _ = await commit_worktree(path, "Agent changes (carried forward)")
                _ = await run_git_ok(path, "checkout", branch)
            else:
                # A directory left behind by a previous run with no .git is not a
                # worktree — remove the registration and re-add rather than failing.
                _ = await run_git(root, "worktree", "prune")
                _ = await run_git_ok(root, "worktree", "add", str(path), branch)

            self._holders[project_id] = conversation_id
            return WorktreeState(path=path, branch=branch, base_ref=base_ref)

    def _require_free(self, project_id: str, conversation_id: str) -> None:
        """Refuse when *another* conversation is working in this project. Called under
        the lock. A conversation's own delegated children pass, which is what lets a
        child checkout exist beside its parent's."""
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
        """Cut this conversation's branch from **another conversation's**, for a fork.

        A forked worktree thread inherits a transcript describing files as they are on the
        source's branch, so branching it from the project's base ref instead would hand it
        a tree that contradicts its own history. Raises when the source has no branch —
        the caller treats that as "nothing to inherit" and lets the fork cut its own
        branch from the base ref on its first coding turn, which is correct: there was
        nothing there to preserve.
        """
        _ = await run_git_ok(root, "rev-parse", "--verify", branch_for(source_id))
        target = branch_for(conversation_id)
        code, _, _ = await run_git(root, "rev-parse", "--verify", target)
        if code == 0:
            return  # already cut — forking twice must not fail the second time
        _ = await run_git_ok(root, "branch", target, branch_for(source_id))

    async def snapshot(self, project_id: str, *, conversation_id: str) -> bool:
        """Commit whatever the agent has changed in the worktree onto its branch.

        **Nothing else commits.** The agent edits files with its file and shell tools and
        has no `git commit` of its own — deliberately, because deciding when work is a
        commit is not something to leave to the model. So the chassis commits, and it does
        it here, right before anything wants to *read* the branch. Without this the whole
        chain downstream is inert in a way that looks like it works: `diff` compares two
        refs, so it would report zero changes after a session that rewrote ten files,
        MERGE would land nothing, and deleting the thread would silently discard
        everything the delete gate exists to protect.

        Returns whether it actually committed. Only the conversation that *holds* the
        checkout may snapshot: committing another thread's leftovers onto this branch
        would attribute work to the wrong conversation.
        """
        if self._holders.get(project_id) != conversation_id:
            return False
        return await commit_worktree(self.path_for(project_id))

    async def diff(
        self, root: Path, *, base_ref: str, conversation_id: str, project_id: str
    ) -> Diff:
        """What this conversation changed: the patch, the per-file rows, and how the
        branch stands against the base.

        Snapshots first, so what the operator reviews includes the work the agent has
        just done rather than only whatever happened to be committed already.

        The counts come from git rather than from walking the patch — the patch may be
        megabytes, and git has already counted. ``-M`` so a moved file reads as a move
        instead of a whole-file delete beside a whole-file add, which is the difference
        between a two-line review and a two-hundred-line one.
        """
        _ = await self.snapshot(project_id, conversation_id=conversation_id)
        branch = branch_for(conversation_id)
        # Three dots: changes on the branch since it diverged, not changes the base has
        # made since — the operator wants to review the agent's work, not their own.
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
        """(ahead, behind) — commits the branch has that the base does not, and the
        other way round.

        ``rev-list --left-right --count base...branch`` prints them as ``behind ahead``:
        the left side is what only the base has. Best-effort — a base ref that has since
        been deleted or renamed is a reason to stop reporting staleness, not a reason to
        fail the whole diff read the operator is standing in front of.
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
        """The branch tip's committer date, ISO 8601 with its offset. None when the
        branch has no commit yet — a thread that acquired a checkout and has not written
        anything."""
        code, out, _ = await run_git(root, "log", "-1", "--format=%cI", branch)
        return out.strip() or None if code == 0 else None

    async def merge(
        self, root: Path, *, base_ref: str, conversation_id: str, project_id: str
    ) -> str:
        """Land the branch on the base ref — the one operation that writes the
        operator's own tree, and the reason the operator presses it themselves.

        **Refuses when their checkout is not on the base ref.** `git merge` lands on
        whatever HEAD happens to be, and the diff they just reviewed was computed against
        `base_ref` — so merging while they sit on some other branch would give them a
        different result from the one they read and approved. Better to say so.

        Snapshots first for the same reason `diff` does, then releases the project so a
        second worktree conversation can take it: without that, a project stays locked to
        its first thread forever and the busy message names an escape that does not work.
        """
        _ = await self.snapshot(project_id, conversation_id=conversation_id)
        await self._require_base_checked_out(root, base_ref)
        branch = branch_for(conversation_id)
        out = await run_git_ok(root, *AUTHOR, "merge", "--no-ff", branch, "-m", f"Merged {branch}")
        self.release(project_id, conversation_id)
        return out

    async def _require_base_checked_out(self, root: Path, base_ref: str) -> None:
        """Refuse a merge onto a ref the operator is not actually standing on.

        `HEAD` is the base ref for a repository we never learned a branch name for, and
        it means "wherever you are" — so it always matches. A detached HEAD reports the
        literal string `HEAD` from `--abbrev-ref`, which is *not* a match for a named
        base, and refusing there is right: a merge into a detached HEAD is lost work.
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
        """Throw the branch away, and every delegated checkout cut from it. Best-effort
        and idempotent — this runs when a conversation is deleted, and a half-set-up
        thread must not block that."""
        await discard_children(
            root=root,
            worktrees_dir=self._root,
            project_id=project_id,
            conversation_id=conversation_id,
        )
        branch = branch_for(conversation_id)
        path = self.path_for(project_id)
        if self._holders.get(project_id) == conversation_id:
            # Throw the working tree away with the branch. Without this the discarded
            # thread's uncommitted files survive the checkout below and get carried onto
            # whichever branch is taken up next — the operator said discard, so discard.
            _ = await run_git(path, "reset", "--hard")
            _ = await run_git(path, "clean", "-fd")
            # Park the worktree off the branch so it can be deleted. `--detach` rather
            # than the base ref by name: the base is normally checked out in the *main*
            # working tree, and git refuses to have one branch checked out twice.
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
    """Pull one number out of `git diff --shortstat`.

    Parsed rather than counted from the patch because the patch may be enormous, and
    git already did the counting. An empty diff yields an empty string, hence the 0.
    """
    for part in shortstat.split(","):
        if word in part:
            digits = "".join(ch for ch in part if ch.isdigit())
            return int(digits) if digits else 0
    return 0
