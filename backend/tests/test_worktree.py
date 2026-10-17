"""Worktrees, against real git.

Mocking git here would test nothing worth testing — the whole point of this layer is
that a real `git worktree add` behaves, that a branch can be checked out twice in a way
git accepts, and that the operator's own tree is genuinely untouched. So these drive the
real binary in a temporary repository.

The load-bearing assertion is the last one: after the agent has edited and committed in
its worktree, the operator's checkout still reads exactly what it did before.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from core.exceptions import InvalidInputError
from services.projects.repo import (
    WorktreeError,
    WorktreeState,
    branch_for,
    child_branch_for,
    run_git,
)
from services.projects.worktree import WorktreeBusyError, WorktreeManager
from services.workspace import WORKTREE_SCRATCH, prepare_worktree_workspace


async def _run(cwd: Path, *args: str) -> None:
    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=str(cwd),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    _ = await proc.communicate()


async def _repo(tmp_path: Path) -> Path:
    """A real repository with one commit."""
    root = tmp_path / "project"
    root.mkdir()
    _ = (root / "hello.txt").write_text("original\n")
    await _run(root, "git", "init", "-b", "main")
    await _run(root, "git", "add", "-A")
    await _run(
        root,
        "git",
        "-c",
        "user.name=T",
        "-c",
        "user.email=t@e",
        "commit",
        "-m",
        "first",
    )
    return root


def _manager(tmp_path: Path) -> WorktreeManager:
    return WorktreeManager(tmp_path / "worktrees")


class TestEnsureRepo:
    async def test_refuses_to_init_without_confirmation(self, tmp_path: Path):
        plain = tmp_path / "plain"
        plain.mkdir()
        # Creating a repository in someone's directory is a real side effect; it must
        # never happen as a side effect of starting a chat.
        with pytest.raises(InvalidInputError):
            _ = await _manager(tmp_path).ensure_repo(plain, confirmed=False)

    async def test_initialises_when_confirmed(self, tmp_path: Path):
        plain = tmp_path / "plain"
        plain.mkdir()
        _ = (plain / "a.txt").write_text("x")
        manager = _manager(tmp_path)

        assert await manager.ensure_repo(plain, confirmed=True) is True
        assert await manager.is_repo(plain) is True
        # Idempotent — a second call is not a second repository.
        assert await manager.ensure_repo(plain, confirmed=True) is False

    async def test_initialises_an_empty_directory(self, tmp_path: Path):
        # --allow-empty matters: with no files there is no commit to branch a worktree
        # from, and acquire would fail on a project that is merely new.
        empty = tmp_path / "empty"
        empty.mkdir()
        manager = _manager(tmp_path)
        _ = await manager.ensure_repo(empty, confirmed=True)
        state = await manager.acquire(
            project_id="p", root=empty, base_ref="HEAD", conversation_id="c1"
        )
        assert state.path.is_dir()


class TestAcquire:
    async def test_checks_the_branch_out_beside_the_repo(self, tmp_path: Path):
        root = await _repo(tmp_path)
        state = await _manager(tmp_path).acquire(
            project_id="p", root=root, base_ref="main", conversation_id="c1"
        )
        assert state.branch == branch_for("c1")
        assert (state.path / "hello.txt").read_text() == "original\n"
        # Outside the repository, and outside data_dir — the host-command fence denies
        # reads of the whole data directory.
        assert root not in state.path.parents

    async def test_is_idempotent_for_the_same_conversation(self, tmp_path: Path):
        root = await _repo(tmp_path)
        manager = _manager(tmp_path)
        first = await manager.acquire(
            project_id="p", root=root, base_ref="main", conversation_id="c1"
        )
        second = await manager.acquire(
            project_id="p", root=root, base_ref="main", conversation_id="c1"
        )
        assert first.path == second.path

    async def test_refuses_a_second_conversation(self, tmp_path: Path):
        root = await _repo(tmp_path)
        manager = _manager(tmp_path)
        _ = await manager.acquire(project_id="p", root=root, base_ref="main", conversation_id="c1")
        # One worktree per project, so two threads cannot interleave edits over one
        # checkout and corrupt each other's picture of the tree.
        with pytest.raises(WorktreeBusyError):
            _ = await manager.acquire(
                project_id="p", root=root, base_ref="main", conversation_id="c2"
            )

    async def test_a_released_project_can_be_taken_by_another_conversation(self, tmp_path: Path):
        root = await _repo(tmp_path)
        manager = _manager(tmp_path)
        _ = await manager.acquire(project_id="p", root=root, base_ref="main", conversation_id="c1")
        manager.release("p", "c1")
        state = await manager.acquire(
            project_id="p", root=root, base_ref="main", conversation_id="c2"
        )
        assert state.branch == branch_for("c2")

    async def test_a_non_repo_is_refused(self, tmp_path: Path):
        plain = tmp_path / "plain"
        plain.mkdir()
        with pytest.raises(InvalidInputError):
            _ = await _manager(tmp_path).acquire(
                project_id="p", root=plain, base_ref="HEAD", conversation_id="c1"
            )


class TestDiffAndMerge:
    async def _edited(self, tmp_path: Path) -> tuple[Path, WorktreeManager, WorktreeState]:
        """A conversation that has done work — **without committing it**.

        That is the whole point. The agent has file and shell tools and no `git commit`,
        so this is exactly the state every real code session is in when the operator
        opens the diff. An earlier version of these tests committed by hand here, and
        every assertion below passed while `diff`/`merge`/the delete gate were inert
        against real usage: a ref-to-ref diff sees only commits.
        """
        root = await _repo(tmp_path)
        manager = _manager(tmp_path)
        state = await manager.acquire(
            project_id="p", root=root, base_ref="main", conversation_id="c1"
        )
        _ = (state.path / "hello.txt").write_text("changed by the agent\n")
        _ = (state.path / "added.txt").write_text("a file the agent created\n")
        return root, manager, state

    async def test_the_operators_tree_is_untouched_until_they_merge(self, tmp_path: Path):
        root, manager, _state = await self._edited(tmp_path)

        # THE assertion this whole design exists for.
        assert (root / "hello.txt").read_text() == "original\n"

        _ = await manager.merge(root, base_ref="main", conversation_id="c1", project_id="p")
        assert (root / "hello.txt").read_text() == "changed by the agent\n"
        # An untracked file the agent created lands too — `add -A`, not `add -u`.
        assert (root / "added.txt").is_file()

    async def test_diff_reports_uncommitted_work(self, tmp_path: Path):
        root, manager, _state = await self._edited(tmp_path)
        diff = await manager.diff(root, base_ref="main", conversation_id="c1", project_id="p")
        assert diff.files_changed == 2
        assert "changed by the agent" in diff.patch
        assert "a file the agent created" in diff.patch

    async def test_the_scratch_directory_never_reaches_the_diff(self, tmp_path: Path):
        root, manager, state = await self._edited(tmp_path)
        prepare_worktree_workspace(state.path)
        (state.path / WORKTREE_SCRATCH / "attachments").mkdir(parents=True, exist_ok=True)
        _ = (state.path / WORKTREE_SCRATCH / "attachments" / "note.txt").write_text("mine")

        diff = await manager.diff(root, base_ref="main", conversation_id="c1", project_id="p")
        # Hephaestus' own staged attachments and skill bundles are not the agent's work,
        # and the operator must not be asked to review them.
        assert WORKTREE_SCRATCH not in diff.patch

    async def test_merge_refuses_when_the_operator_is_on_another_branch(self, tmp_path: Path):
        root, manager, _state = await self._edited(tmp_path)
        await _run(root, "git", "checkout", "-b", "somewhere-else")

        # The diff was computed against `main`; merging here would land a different
        # result from the one they read.
        with pytest.raises(WorktreeError):
            _ = await manager.merge(root, base_ref="main", conversation_id="c1", project_id="p")
        assert (root / "hello.txt").read_text() == "original\n"

    async def test_merging_hands_the_project_back(self, tmp_path: Path):
        root, manager, _state = await self._edited(tmp_path)
        _ = await manager.merge(root, base_ref="main", conversation_id="c1", project_id="p")
        # Otherwise a project stays locked to its first code thread forever, and the
        # busy message tells the operator to do the thing they just did.
        assert manager.holder("p") is None
        state = await manager.acquire(
            project_id="p", root=root, base_ref="main", conversation_id="c2"
        )
        assert state.branch == branch_for("c2")

    async def test_discard_removes_the_branch_and_its_working_files(self, tmp_path: Path):
        root, manager, state = await self._edited(tmp_path)
        await manager.discard(root, project_id="p", conversation_id="c1")
        # And the operator's tree still never saw it.
        assert (root / "hello.txt").read_text() == "original\n"
        assert manager.holder("p") is None
        # The discarded thread's files are gone from the checkout too — left behind,
        # they would be carried onto whichever branch is taken up next.
        assert not (state.path / "added.txt").exists()

    async def test_a_second_conversation_does_not_inherit_the_first_ones_edits(
        self, tmp_path: Path
    ):
        root, manager, state = await self._edited(tmp_path)
        manager.release("p", "c1")

        _ = await manager.acquire(project_id="p", root=root, base_ref="main", conversation_id="c2")
        # A `git checkout` carries uncommitted files across, so without committing the
        # outgoing branch first, c1's half-finished work would land on c2's branch.
        diff = await manager.diff(root, base_ref="main", conversation_id="c2", project_id="p")
        assert diff.files_changed == 0
        assert (state.path / "hello.txt").read_text() == "original\n"


class TestChildWorktree:
    """A sub-agent's own worktree, and how its work lands."""

    async def _forked(self, tmp_path: Path):
        """A parent mid-session — edits uncommitted, dependencies installed — and the
        child cut from its worktree."""
        root = await _repo(tmp_path)
        manager = _manager(tmp_path)
        parent = await manager.acquire(
            project_id="p", root=root, base_ref="main", conversation_id="c1"
        )
        _ = (parent.path / "hello.txt").write_text("the parent was in the middle of this\n")
        # Ignored, the way a real project ignores them: git carries neither across, so
        # the cut's own copy is the only thing that can.
        _ = (parent.path / ".gitignore").write_text("node_modules/\n.venv/\n")
        (parent.path / "node_modules").mkdir()
        _ = (parent.path / "node_modules" / "dep.js").write_text("minutes of installing")
        (parent.path / ".venv").mkdir()
        _ = (parent.path / ".venv" / "pyvenv.cfg").write_text(f"home = {parent.path}/.venv/bin")
        child = await manager.cut_child(
            project_id="p", root=root, conversation_id="c1", delegation_id="d1", source=parent
        )
        return root, manager, parent, child

    async def _from_checkout(self, tmp_path: Path):
        """The operator mid-edit in their own checkout, and a child cut from it."""
        root = await _repo(tmp_path)
        manager = _manager(tmp_path)
        _ = (root / "hello.txt").write_text("the operator's unsaved thought\n")
        _ = (root / "notes.txt").write_text("not even added yet\n")
        child = await manager.cut_child(
            project_id="p", root=root, conversation_id="c1", delegation_id="d1", source=None
        )
        return root, manager, child

    async def _changes(self, manager: WorktreeManager, base_ref: str):
        return await manager.child_changes(
            project_id="p", conversation_id="c1", delegation_id="d1", base_ref=base_ref
        )

    async def _apply(self, manager: WorktreeManager, root: Path, base_ref: str):
        return await manager.apply_child(
            project_id="p", root=root, conversation_id="c1", delegation_id="d1", base_ref=base_ref
        )

    async def test_a_child_opens_on_the_parents_uncommitted_edits(self, tmp_path: Path):
        _root, manager, parent, child = await self._forked(tmp_path)

        # THE assertion: the child was handed the files the parent's transcript
        # describes, not whatever happened to be committed.
        assert (child.path / "hello.txt").read_text() == "the parent was in the middle of this\n"
        assert child.path != parent.path
        assert child.branch == child_branch_for("c1", "d1")
        # Warm: reinstalling the parent's dependencies before the first command is the
        # cost that stops anyone from delegating.
        assert (child.path / "node_modules" / "dep.js").read_text() == "minutes of installing"
        # But not the virtualenv: it is bound to the path it was built at, so a cloned
        # one runs the parent's interpreter and installs into the parent's environment.
        assert not (child.path / ".venv").exists()
        # A child is the same conversation working in two places — it must not read as a
        # second thread taking the project.
        assert manager.holder("p") == "c1"
        # Its own work starts at the cut: nothing yet.
        assert (await self._changes(manager, child.base_ref)).files == []

    async def test_reopening_reads_the_same_child_off_the_disk(self, tmp_path: Path):
        _root, manager, _parent, child = await self._forked(tmp_path)
        again = manager.reopen_child(project_id="p", conversation_id="c1", delegation_id="d1")
        assert (again.path, again.branch) == (child.path, child.branch)

    async def test_a_clean_merge_lands_in_the_parent_and_retires_the_child(self, tmp_path: Path):
        root, manager, parent, child = await self._forked(tmp_path)
        _ = (child.path / "added.txt").write_text("the delegated agent's work\n")

        report = await manager.merge_child(
            project_id="p", root=root, conversation_id="c1", delegation_id="d1", into=parent.path
        )

        assert report.merged
        assert "added.txt" in report.files
        assert (parent.path / "added.txt").read_text() == "the delegated agent's work\n"
        assert not child.path.exists()  # the checkout is retired with the branch
        code, _, _ = await run_git(root, "rev-parse", "--verify", child.branch)
        assert code != 0
        # And a merge back is not a merge: the operator's own tree never saw any of it.
        assert (root / "hello.txt").read_text() == "original\n"

    async def test_a_conflict_aborts_and_keeps_the_child_to_look_at(self, tmp_path: Path):
        root, manager, parent, child = await self._forked(tmp_path)
        _ = (parent.path / "hello.txt").write_text("what the parent did while it waited\n")
        _ = (child.path / "hello.txt").write_text("what the child did\n")

        report = await manager.merge_child(
            project_id="p", root=root, conversation_id="c1", delegation_id="d1", into=parent.path
        )

        assert not report.merged
        assert report.conflicts == ["hello.txt"]
        # The parent's tree is left usable: its own version, no conflict markers, no
        # half-finished merge for its next command to trip over.
        assert (parent.path / "hello.txt").read_text() == "what the parent did while it waited\n"
        code, _, _ = await run_git(parent.path, "rev-parse", "--verify", "MERGE_HEAD")
        assert code != 0
        # The work is still reachable — refusing to merge must not also throw it away.
        assert (child.path / "hello.txt").read_text() == "what the child did\n"
        kept, _, _ = await run_git(root, "rev-parse", "--verify", child.branch)
        assert kept == 0

    async def test_a_deletion_comes_back_named_rather_than_folded_into_the_changes(
        self, tmp_path: Path
    ):
        # "Your file changed" and "your file is gone" are not the same sentence, and the
        # caller must never have to infer the second one.
        _root, manager, _parent, child = await self._forked(tmp_path)
        (child.path / "hello.txt").unlink()

        report = await self._changes(manager, child.base_ref)

        assert report.deleted == ["hello.txt"]
        assert report.files == []

    async def test_a_child_cut_from_the_checkout_starts_on_its_unsaved_files(self, tmp_path: Path):
        root, manager, child = await self._from_checkout(tmp_path)

        assert (child.path / "hello.txt").read_text() == "the operator's unsaved thought\n"
        assert (child.path / "notes.txt").read_text() == "not even added yet\n"
        # The operator's own git state is untouched by the cut.
        _code, status, _err = await run_git(root, "status", "--porcelain")
        assert sorted(line[3:] for line in status.splitlines()) == ["hello.txt", "notes.txt"]
        # And those files are its starting point, not its work.
        assert (await self._changes(manager, child.base_ref)).files == []

    async def test_applying_lands_only_the_childs_work_and_writes_no_git_state(
        self, tmp_path: Path
    ):
        root, manager, child = await self._from_checkout(tmp_path)
        _ = (child.path / "added.txt").write_text("the sub-agent's work\n")
        head_before = await run_git(root, "rev-parse", "HEAD")

        report = await self._apply(manager, root, child.base_ref)

        assert report.merged
        assert report.files == ["added.txt"]
        assert (root / "added.txt").read_text() == "the sub-agent's work\n"
        assert (root / "hello.txt").read_text() == "the operator's unsaved thought\n"
        assert await run_git(root, "rev-parse", "HEAD") == head_before
        staged, _, _ = await run_git(root, "diff", "--cached", "--quiet")
        assert staged == 0
        assert not child.path.exists()

    async def test_applying_a_clash_changes_nothing_and_keeps_the_child(self, tmp_path: Path):
        root, manager, child = await self._from_checkout(tmp_path)
        _ = (child.path / "hello.txt").write_text("the sub-agent's version\n")
        _ = (root / "hello.txt").write_text("the operator kept typing\n")

        report = await self._apply(manager, root, child.base_ref)

        assert not report.merged
        assert report.conflicts == ["hello.txt"]
        assert (root / "hello.txt").read_text() == "the operator kept typing\n"
        assert (child.path / "hello.txt").read_text() == "the sub-agent's version\n"

    async def test_a_non_repository_has_no_checkout_to_cut_from(self, tmp_path: Path):
        plain = tmp_path / "plain"
        plain.mkdir()
        with pytest.raises(WorktreeError):
            _ = await _manager(tmp_path).cut_child(
                project_id="p", root=plain, conversation_id="c1", delegation_id="d1", source=None
            )

    async def test_discarding_one_child_takes_its_branch_with_it(self, tmp_path: Path):
        # Idempotent: a clean merge may already have retired the same child.
        root, manager, _parent, child = await self._forked(tmp_path)
        for _ in range(2):
            await manager.discard_child(
                project_id="p", root=root, conversation_id="c1", delegation_id="d1"
            )

        assert not child.path.exists()
        code, _, _ = await run_git(root, "rev-parse", "--verify", child.branch)
        assert code != 0
        # The parent is untouched — a discarded child is not a discarded conversation.
        assert manager.holder("p") == "c1"
        with pytest.raises(WorktreeError):
            _ = manager.reopen_child(project_id="p", conversation_id="c1", delegation_id="d1")

    async def test_discarding_the_conversation_takes_its_delegated_checkouts_with_it(
        self, tmp_path: Path
    ):
        root, manager, _parent, child = await self._forked(tmp_path)

        await manager.discard(root, project_id="p", conversation_id="c1")

        assert not child.path.exists()
        code, _, _ = await run_git(root, "rev-parse", "--verify", child.branch)
        assert code != 0
        _code, registered, _err = await run_git(root, "worktree", "list")
        assert str(child.path) not in registered


class TestMissingDirectory:
    async def test_a_project_that_moved_reads_as_a_git_failure_not_a_crash(self, tmp_path: Path):
        # The operator can move or delete a project directory at any time; every caller
        # here handles "git said no" and none expects an OSError.
        manager = _manager(tmp_path)
        assert await manager.is_repo(tmp_path / "gone") is False
        with pytest.raises(WorktreeError):
            _ = await manager.diff(
                tmp_path / "gone", base_ref="main", conversation_id="c1", project_id="p"
            )
