"""Working in a project's own folder, and the agent's way out of it into a worktree."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from pydantic_ai import RunContext
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage

from core.container import ServiceContainer
from core.db import init_db, make_engine
from core.vault import Vault
from runs import Run, RunStatus, RunStream
from services.conversations import ConversationStore
from services.projects import ProjectStore, WorktreeManager
from services.projects.checkout import CheckoutBusyError, CheckoutHolders
from services.workspace import WORKTREE_SCRATCH
from services.worktree_switch import WorktreeSwitch
from tools.deps import RunDeps
from tools.projects import project_toolset
from tools.workspace import run_workspace

OWNER = "operator"


class _FakeProjects:
    def __init__(self, roots: dict[str, Path]) -> None:
        self._roots = roots

    async def get(self, owner_id: str, project_id: str):
        class _View:
            root_path = str(self._roots[project_id])
            base_ref = "main"

        return _View()


class _FakeSwitch:
    def __init__(self) -> None:
        self.opened: list[str] = []

    async def open(self, conversation_id: str) -> bool:
        self.opened.append(conversation_id)
        return True


async def _git(cwd: Path, *args: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate()
    return out.decode()


async def _repo(root: Path) -> Path:
    root.mkdir(parents=True)
    (root / "hello.txt").write_text("original\n")
    await _git(root, "init", "-b", "main")
    await _git(root, "add", "-A")
    await _git(root, "-c", "user.name=T", "-c", "user.email=t@e", "commit", "-m", "first")
    return root


def _caps(tmp_path: Path, root: Path, switch: _FakeSwitch | None = None) -> ServiceContainer:
    caps = ServiceContainer()
    caps.add(_FakeProjects({"proj-1": root}), as_type=ProjectStore)
    caps.add(WorktreeManager(tmp_path / "worktrees"), as_type=WorktreeManager)
    caps.add(CheckoutHolders())
    if switch is not None:
        caps.add(switch, as_type=WorktreeSwitch)
    return caps


def _run(run_id: str = "run-1") -> Run:
    return Run(id=run_id, kind="chat", owner_id=OWNER, stream=RunStream())


def _ctx(caps: ServiceContainer, *, run: Run | None = None, **deps) -> RunContext[RunDeps]:
    deps.setdefault("conversation_id", "conv-a")
    deps.setdefault("project_id", "proj-1")
    deps.setdefault("workspace_kind", "checkout")
    return RunContext(
        deps=RunDeps(run=run or _run(), owner_id=OWNER, caps=caps, **deps),
        model=TestModel(),
        usage=RunUsage(),
    )


class TestTheCheckout:
    async def test_it_is_the_operators_own_folder_on_its_branch(self, tmp_path):
        root = await _repo(tmp_path / "project")
        workspace = await run_workspace(_ctx(_caps(tmp_path, root)))
        assert workspace is not None
        assert workspace.kind == "checkout"
        assert workspace.root == root
        assert workspace.branch == "main"
        assert workspace.display("hello.txt") == str(root / "hello.txt")

    async def test_staged_files_stay_out_of_the_operators_diff(self, tmp_path):
        root = await _repo(tmp_path / "project")
        workspace = await run_workspace(_ctx(_caps(tmp_path, root)))
        assert workspace is not None
        workspace.files.write_file(f"{workspace.stage_prefix}attachments/a.txt", b"hi")
        assert WORKTREE_SCRATCH not in await _git(root, "status", "--porcelain")

    async def test_a_second_conversation_is_refused_while_the_first_is_working(self, tmp_path):
        root = await _repo(tmp_path / "project")
        caps = _caps(tmp_path, root)
        first = _run("run-1")
        assert await run_workspace(_ctx(caps, run=first)) is not None
        with pytest.raises(CheckoutBusyError):
            await run_workspace(_ctx(caps, run=_run("run-2"), conversation_id="conv-b"))

    async def test_the_hold_ends_with_the_run_that_took_it(self, tmp_path):
        root = await _repo(tmp_path / "project")
        caps = _caps(tmp_path, root)
        first = _run("run-1")
        await run_workspace(_ctx(caps, run=first))
        first.status = RunStatus.done
        other = await run_workspace(_ctx(caps, run=_run("run-2"), conversation_id="conv-b"))
        assert other is not None and other.root == root

    async def test_a_delegated_run_shares_its_parents_hold(self, tmp_path):
        root = await _repo(tmp_path / "project")
        caps = _caps(tmp_path, root)
        await run_workspace(_ctx(caps, run=_run("run-1")))
        child = _ctx(caps, run=_run("run-2"), conversation_id="child", workspace_key="conv-a")
        workspace = await run_workspace(child)
        assert workspace is not None and workspace.root == root


async def _open_worktree(ctx: RunContext[RunDeps]) -> str:
    toolset = project_toolset()
    tools = await toolset.get_tools(ctx)
    return await toolset.call_tool("open_worktree", {}, ctx, tools["open_worktree"])


class TestOpeningAWorktree:
    async def test_it_moves_the_run_onto_its_own_branch_mid_turn(self, tmp_path):
        root = await _repo(tmp_path / "project")
        switch = _FakeSwitch()
        ctx = _ctx(_caps(tmp_path, root, switch))
        before = await run_workspace(ctx)
        assert before is not None and before.kind == "checkout"

        result = await _open_worktree(ctx)

        assert switch.opened == ["conv-a"]
        assert ctx.deps.workspace_kind == "worktree"
        after = await run_workspace(ctx)
        assert after is not None
        assert after.kind == "worktree"
        assert after.root != root
        assert after.branch == "ody/conv-a"
        assert "ody/conv-a" in result

    async def test_an_unfiled_chat_has_nothing_to_check_out(self, tmp_path):
        root = await _repo(tmp_path / "project")
        switch = _FakeSwitch()
        ctx = _ctx(_caps(tmp_path, root, switch), project_id=None, workspace_kind="sandbox")
        assert "not bound to a project" in await _open_worktree(ctx)
        assert switch.opened == []
        assert ctx.deps.workspace_kind == "sandbox"

    async def test_a_delegated_agent_cannot_move_its_parents_workspace(self, tmp_path):
        root = await _repo(tmp_path / "project")
        switch = _FakeSwitch()
        ctx = _ctx(_caps(tmp_path, root, switch), conversation_id="child", workspace_key="conv-a")
        assert "delegated" in await _open_worktree(ctx)
        assert switch.opened == []

    async def test_a_worktree_chat_is_told_so(self, tmp_path):
        root = await _repo(tmp_path / "project")
        switch = _FakeSwitch()
        ctx = _ctx(_caps(tmp_path, root, switch), workspace_kind="worktree")
        assert "already" in await _open_worktree(ctx)
        assert switch.opened == []


class TestTheStoreRemembersTheMove:
    async def _store(self, tmp_path) -> ConversationStore:
        engine = make_engine("sqlite:///:memory:")
        init_db(engine)
        vault = Vault(tmp_path / "keyfile.json")
        await vault.setup("pw")
        return ConversationStore(engine, vault)

    async def test_a_project_thread_moves_to_a_worktree_and_stays(self, tmp_path):
        store = await self._store(tmp_path)
        conversation_id = await store.create_conversation(
            OWNER, project_id="p1", workspace_kind="checkout"
        )
        assert (await store.binding(conversation_id)).workspace_kind == "checkout"
        assert await store.open_worktree(conversation_id)
        binding = await store.binding(conversation_id)
        assert binding.workspace_kind == "worktree"
        assert binding.project_id == "p1"

    async def test_an_unfiled_thread_cannot_move(self, tmp_path):
        store = await self._store(tmp_path)
        conversation_id = await store.create_conversation(OWNER)
        assert not await store.open_worktree(conversation_id)
        assert (await store.binding(conversation_id)).workspace_kind == "sandbox"
