"""The workspace-key convention: ``<owner>/<delegation>`` names a sub-agent working in a
worktree of its own, under the thread ``<owner>``, whose scratch it shares.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import final, override

from services.home.scratch import HostFiles, ScratchDirs
from services.projects.repo import WorktreeState, child_branch_for
from services.projects.worktree import WorktreeManager
from services.workspace import sandbox_workspace, scratch_key, split_workspace_key


async def _run(cwd: Path, *args: str) -> None:
    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=str(cwd),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    _ = await proc.communicate()


async def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    _ = (root / "hello.txt").write_text("original\n")
    await _run(root, "git", "init", "-b", "main")
    await _run(root, "git", "add", "-A")
    await _run(root, "git", "-c", "user.name=T", "-c", "user.email=t@e", "commit", "-m", "first")
    return root


class TestSplittingAKey:
    def test_a_plain_key_names_its_own_workspace(self):
        assert split_workspace_key("c123") == ("c123", None)

    def test_a_delegated_key_names_the_owner_and_the_delegation(self):
        assert split_workspace_key("c123/w7") == ("c123", "w7")

    def test_an_empty_key_names_nothing(self):
        # A stateless run has no conversation to key on; the caller falls back rather
        # than resolving a workspace for the empty string.
        assert split_workspace_key("") == ("", None)

    def test_a_trailing_separator_is_not_a_delegation(self):
        # Otherwise a key built by joining an empty id would silently resolve to a
        # *different* checkout than the conversation's own.
        assert split_workspace_key("c123/") == ("c123", None)

    def test_a_delegation_works_in_its_owners_scratch(self):
        assert scratch_key("c123/w7") == "c123"
        assert scratch_key("c123") == "c123"


class TestReopeningAChildWorktree:
    async def _cut(self, tmp_path: Path) -> tuple[WorktreeManager, WorktreeState, WorktreeState]:
        root = await _repo(tmp_path)
        manager = WorktreeManager(tmp_path / "worktrees")
        parent = await manager.acquire(
            project_id="p", root=root, base_ref="main", conversation_id="c1"
        )
        _ = (parent.path / "hello.txt").write_text("mid-edit\n")
        child = await manager.cut_child(
            project_id="p", root=root, conversation_id="c1", delegation_id="d1", source=parent
        )
        return manager, parent, child

    async def test_reopening_keeps_the_childs_own_work(self, tmp_path: Path):
        manager, _parent, child = await self._cut(tmp_path)
        _ = (child.path / "wip.txt").write_text("half-done\n")

        again = manager.reopen_child(project_id="p", conversation_id="c1", delegation_id="d1")

        # A sub-agent resolves its workspace on every file-tool call, so every answer has
        # to be the same checkout with its work still in it.
        assert again.path == child.path
        assert again.branch == child_branch_for("c1", "d1")
        assert (again.path / "wip.txt").read_text() == "half-done\n"

    async def test_reopening_does_not_commit_the_parent_again(self, tmp_path: Path):
        manager, parent, _child = await self._cut(tmp_path)
        before = await _log(parent.path)

        for _ in range(3):
            _ = manager.reopen_child(project_id="p", conversation_id="c1", delegation_id="d1")

        assert await _log(parent.path) == before

    async def test_two_children_of_one_parent_get_their_own_worktrees(self, tmp_path: Path):
        manager, parent, one = await self._cut(tmp_path)
        two = await manager.cut_child(
            project_id="p",
            root=tmp_path / "project",
            conversation_id="c1",
            delegation_id="d2",
            source=parent,
        )
        assert one.path != two.path


@final
class _RecordingScratch(ScratchDirs):
    """Records which keys `sandbox_workspace` opened."""

    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.acquired: list[str] = []

    @override
    def acquire(self, key: str) -> HostFiles:
        self.acquired.append(key)
        return super().acquire(key)


class TestOpeningTheScratch:
    def test_an_ordinary_key_is_the_conversations_own_scratch(self, tmp_path: Path):
        scratch = _RecordingScratch(tmp_path)
        _ = sandbox_workspace(scratch, "c1")
        assert scratch.acquired == ["c1"]

    def test_a_delegated_key_opens_its_owners_scratch(self, tmp_path: Path):
        scratch = _RecordingScratch(tmp_path)

        workspace = sandbox_workspace(scratch, "c1/s-ab12")

        assert scratch.acquired == ["c1"]
        assert workspace.root == scratch.path("c1")


async def _log(path: Path) -> str:
    proc = await asyncio.create_subprocess_exec(
        "git",
        "log",
        "--oneline",
        cwd=str(path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate()
    return out.decode()
