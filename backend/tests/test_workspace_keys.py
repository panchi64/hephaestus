"""One workspace-key convention, honoured by both workspace kinds.

A workspace key has always been able to name a *delegation* of a workspace rather than the
workspace itself — a forked sandbox is keyed ``<parent>/<child>`` — but only the sandbox
half read it that way. The worktree half keyed off the conversation instead, which is why a
delegated run in a project folder could be handed its own container and never its own checkout:
its conversation does not hold the project, so it was refused as a rival for it.

These hold the two halves of that: that the split is read the same way everywhere, and that
an ordinary run — whose key *is* its conversation — resolves exactly as it did before.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import cast

import pytest

from services.projects.repo import child_branch_for
from services.projects.worktree import WorktreeManager
from services.sandbox import LiveWork, SandboxSessionManager
from services.workspace import RunWorkspace, sandbox_workspace, split_workspace_key


async def _run(cwd, *args: str) -> None:
    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=str(cwd),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.communicate()


async def _repo(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "hello.txt").write_text("original\n")
    await _run(root, "git", "init", "-b", "main")
    await _run(root, "git", "add", "-A")
    await _run(root, "git", "-c", "user.name=T", "-c", "user.email=t@e", "commit", "-m", "first")
    return root


class TestSplittingAKey:
    def test_a_plain_key_names_its_own_workspace(self):
        assert split_workspace_key("c123") == ("c123", None)

    def test_a_delegated_key_names_the_parents_workspace_and_the_delegation(self):
        assert split_workspace_key("c123/w7") == ("c123", "w7")

    def test_an_empty_key_names_nothing(self):
        # A stateless run has no conversation to key on; the caller falls back rather
        # than resolving a workspace for the empty string.
        assert split_workspace_key("") == ("", None)

    def test_a_trailing_separator_is_not_a_delegation(self):
        # Otherwise a key built by joining an empty id would silently resolve to a
        # *different* checkout than the conversation's own.
        assert split_workspace_key("c123/") == ("c123", None)


class TestOpeningAChildCheckout:
    async def _held(self, tmp_path):
        root = await _repo(tmp_path)
        manager = WorktreeManager(tmp_path / "worktrees")
        parent = await manager.acquire(
            project_id="p", root=root, base_ref="main", conversation_id="c1"
        )
        return root, manager, parent

    async def test_a_child_opens_beside_its_parent_without_taking_the_project(self, tmp_path):
        root, manager, parent = await self._held(tmp_path)
        (parent.path / "hello.txt").write_text("mid-edit\n")

        child = await manager.open_child(
            project_id="p", root=root, conversation_id="c1", delegation_id="d1"
        )

        assert child.path != parent.path
        assert child.branch == child_branch_for("c1", "d1")
        # It opens on what the parent's transcript describes, not on the last commit.
        assert (child.path / "hello.txt").read_text() == "mid-edit\n"
        # And the parent still holds the project: a sub-agent working beside the thread
        # that launched it is one conversation in two places, not a second thread.
        assert manager.holder("p") == "c1"

    async def test_reopening_keeps_the_childs_own_work(self, tmp_path):
        root, manager, _parent = await self._held(tmp_path)
        child = await manager.open_child(
            project_id="p", root=root, conversation_id="c1", delegation_id="d1"
        )
        (child.path / "wip.txt").write_text("half-done\n")

        again = await manager.open_child(
            project_id="p", root=root, conversation_id="c1", delegation_id="d1"
        )

        # THE assertion: a sub-agent resolves its workspace on every file-tool call, so
        # the second answer has to be the same checkout with its work still in it.
        assert again.path == child.path
        assert (again.path / "wip.txt").read_text() == "half-done\n"

    async def test_reopening_does_not_commit_the_parent_again(self, tmp_path):
        root, manager, parent = await self._held(tmp_path)
        await manager.open_child(
            project_id="p", root=root, conversation_id="c1", delegation_id="d1"
        )
        before = await _log(parent.path)

        for _ in range(3):
            await manager.open_child(
                project_id="p", root=root, conversation_id="c1", delegation_id="d1"
            )

        # Cutting a fork commits the parent's working tree so the child opens on it.
        # Doing that per resolution would be a commit per tool call on the operator's
        # branch — which is the whole reason this is not just `fork`.
        assert await _log(parent.path) == before

    async def test_two_children_of_one_parent_get_their_own_checkouts(self, tmp_path):
        root, manager, _parent = await self._held(tmp_path)

        one = await manager.open_child(
            project_id="p", root=root, conversation_id="c1", delegation_id="d1"
        )
        two = await manager.open_child(
            project_id="p", root=root, conversation_id="c1", delegation_id="d2"
        )

        # Parallel sub-agents are the ordinary case now — the model launches several in
        # one step — so two of them must never land in one working tree.
        assert one.path != two.path


class _Work:
    is_terminal = False


class _FakeSession:
    """Only what resolving a workspace asks of a session."""

    def __init__(self, key: str, root: Path, *, ephemeral: bool = False) -> None:
        self.key = key
        self.root = root
        self.ephemeral = ephemeral
        self.holders: list[LiveWork | None] = []
        self.touches = 0

    def ensure_workspace(self):
        self.root.mkdir(parents=True, exist_ok=True)
        return self.root

    def hold(self, holder: LiveWork | None) -> None:
        self.holders.append(holder)

    def touch(self) -> None:
        self.touches += 1


class _FakeSessions:
    """A sandbox manager as `sandbox_workspace` uses one, counting what it was asked for.

    Deliberately a fake rather than the real manager: the point of these is *which call*
    a delegated key produces, and the real one wants a container runtime to answer.
    """

    def __init__(self, tmp_path) -> None:
        self._root = tmp_path
        self.sessions: dict[str, _FakeSession] = {}
        self.acquired: list[str] = []
        self.forked: list[tuple[str, str]] = []

    def existing(self, key: str):
        return self.sessions.get(key)

    async def acquire(self, key: str, *, holder: LiveWork | None = None) -> _FakeSession:
        self.acquired.append(key)
        session = self.sessions.setdefault(
            key, _FakeSession(key, self._root / key.replace("/", "_"))
        )
        session.hold(holder)
        return session

    async def fork(
        self, parent_key: str, child_key: str, *, holder: LiveWork | None = None
    ) -> _FakeSession:
        if child_key in self.sessions:
            raise AssertionError(f"already forked {child_key!r}")
        self.forked.append((parent_key, child_key))
        session = _FakeSession(child_key, self._root / child_key.replace("/", "_"), ephemeral=True)
        session.hold(holder)
        self.sessions[child_key] = session
        return session


async def _open(sessions: object, key: str, *, holder: LiveWork | None = None) -> RunWorkspace:
    return await sandbox_workspace(cast(SandboxSessionManager, sessions), key, holder=holder)


class TestOpeningADelegatedSandbox:
    """The sandbox half of the same convention.

    The worktree half above was the one that had to be taught the key; this half was
    always described as reading it, and did not. A delegated key through plain `acquire`
    is the quiet failure: it answers with a session, so nothing raises — the sub-agent is
    simply working in a copy of *nothing*, and the merge at the end finds no fork to land.
    """

    async def test_an_ordinary_key_is_the_conversations_own_session(self, tmp_path):
        sessions = _FakeSessions(tmp_path)

        await _open(sessions, "c1")

        assert sessions.acquired == ["c1"]
        assert sessions.forked == []

    async def test_a_delegated_key_is_a_fork_of_the_workspace_it_delegates_from(self, tmp_path):
        sessions = _FakeSessions(tmp_path)

        workspace = await _open(sessions, "c1/s-ab12")

        assert sessions.forked == [("c1", "c1/s-ab12")]
        # And never as a session of its own: that is the empty workspace.
        assert sessions.acquired == []
        assert workspace.kind == "sandbox"

    async def test_resolving_it_again_reopens_the_same_fork(self, tmp_path):
        sessions = _FakeSessions(tmp_path)

        first = await _open(sessions, "c1/s-ab12")
        second = await _open(sessions, "c1/s-ab12")

        # A delegated run resolves its workspace on every file-tool call, not once at
        # launch — the fake raises on a second fork, which is what the real manager does.
        assert sessions.forked == [("c1", "c1/s-ab12")]
        assert first.root == second.root

    async def test_reopening_claims_the_fork_for_the_run_using_it(self, tmp_path):
        sessions = _FakeSessions(tmp_path)
        holder = _Work()

        await _open(sessions, "c1/s-ab12", holder=holder)
        await _open(sessions, "c1/s-ab12", holder=holder)

        # Displacing a fork *deletes* it, nothing about a fork being sealed — so a copy
        # left unclaimed between two of a sub-agent's tool calls is its work thrown away
        # by the live-session cap.
        assert sessions.sessions["c1/s-ab12"].holders == [holder, holder]

    async def test_a_session_that_is_not_a_fork_is_never_adopted(self, tmp_path):
        sessions = _FakeSessions(tmp_path)
        # Something non-ephemeral squatting the child key — adopting it would mean
        # merging a whole conversation's workspace into another's when this ends.
        sessions.sessions["c1/s-ab12"] = _FakeSession("c1/s-ab12", tmp_path / "squatter")

        with pytest.raises(AssertionError):
            await _open(sessions, "c1/s-ab12")


async def _log(path) -> str:
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
