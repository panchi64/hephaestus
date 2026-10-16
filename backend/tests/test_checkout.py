"""Working in a project's own folder, and the agent's way out of it into a worktree."""

from __future__ import annotations

import asyncio
from pathlib import Path

from pydantic_ai import RunContext
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage

from core.container import ServiceContainer
from core.db import init_db, make_engine
from core.vault import Vault
from runs import Run, RunStream
from services.conversations import ConversationStore
from services.projects import ProjectStore, WorktreeManager
from services.workspace import WORKTREE_SCRATCH
from services.worktree_switch import WorktreeSwitch
from tools.deps import RunDeps
from tools.home import resolve_run_home
from tools.projects import project_toolset
from tools.workspace import run_workspace

from ._json import JSON, s

OWNER = "operator"


class _View:
    def __init__(self, root_path: str) -> None:
        self.root_path: str = root_path
        self.base_ref: str = "main"


class _FakeProjects:
    def __init__(self, roots: dict[str, Path]) -> None:
        self._roots: dict[str, Path] = roots

    async def get(self, _owner_id: str, project_id: str) -> _View:
        return _View(str(self._roots[project_id]))

    async def slugs(self, _owner_id: str) -> dict[str, str]:
        return {project_id: project_id for project_id in self._roots}

    async def workspace_roots(self, _owner_id: str) -> dict[str, Path]:
        return dict(self._roots)


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
    _ = (root / "hello.txt").write_text("original\n")
    _ = await _git(root, "init", "-b", "main")
    _ = await _git(root, "add", "-A")
    _ = await _git(root, "-c", "user.name=T", "-c", "user.email=t@e", "commit", "-m", "first")
    return root


def _caps(tmp_path: Path, root: Path, switch: _FakeSwitch | None = None) -> ServiceContainer:
    caps = ServiceContainer()
    caps.add(_FakeProjects({"proj-1": root}), as_type=ProjectStore)
    caps.add(WorktreeManager(tmp_path / "worktrees"), as_type=WorktreeManager)
    if switch is not None:
        caps.add(switch, as_type=WorktreeSwitch)
    return caps


def _run(run_id: str = "run-1") -> Run:
    return Run(id=run_id, kind="chat", owner_id=OWNER, stream=RunStream())


def _ctx(
    caps: ServiceContainer,
    *,
    run: Run | None = None,
    conversation_id: str = "conv-a",
    project_id: str | None = "proj-1",
    worktree: bool = False,
    workspace_key: str = "",
) -> RunContext[RunDeps]:
    return RunContext(
        deps=RunDeps(
            run=run or _run(),
            owner_id=OWNER,
            caps=caps,
            conversation_id=conversation_id,
            project_id=project_id,
            worktree=worktree,
            workspace_key=workspace_key,
        ),
        model=TestModel(),
        usage=RunUsage(),
    )


class TestTheCheckout:
    async def test_it_is_the_operators_own_folder_on_its_branch(self, tmp_path: Path):
        root = await _repo(tmp_path / "project")
        workspace = await run_workspace(_ctx(_caps(tmp_path, root)))
        assert workspace is not None
        assert workspace.kind == "checkout"
        assert workspace.root == root
        assert workspace.branch == "main"
        assert workspace.display("hello.txt") == "/home/agent/projects/proj-1/hello.txt"

    async def test_staged_files_stay_out_of_the_operators_diff(self, tmp_path: Path):
        root = await _repo(tmp_path / "project")
        workspace = await run_workspace(_ctx(_caps(tmp_path, root)))
        assert workspace is not None
        workspace.files.write_file(f"{workspace.stage_prefix}attachments/a.txt", b"hi")
        assert WORKTREE_SCRATCH not in await _git(root, "status", "--porcelain")

    async def test_two_threads_work_in_one_folder_at_once(self, tmp_path: Path):
        # Writes are fenced by leases and serialised per copy, not by one holder.
        root = await _repo(tmp_path / "project")
        caps = _caps(tmp_path, root)
        first = await run_workspace(_ctx(caps, run=_run("run-1")))
        other = await run_workspace(_ctx(caps, run=_run("run-2"), conversation_id="conv-b"))
        assert first is not None and other is not None and other.root == first.root == root


async def _open_worktree(ctx: RunContext[RunDeps]) -> str:
    toolset = project_toolset()
    tools = await toolset.get_tools(ctx)
    return s(
        JSON.validate_python(
            await toolset.call_tool("open_worktree", {}, ctx, tools["open_worktree"])
        )
    )


class TestOpeningAWorktree:
    async def test_it_gives_the_run_a_branch_of_its_own_mid_turn(self, tmp_path: Path):
        root = await _repo(tmp_path / "project")
        switch = _FakeSwitch()
        ctx = _ctx(_caps(tmp_path, root, switch))
        before = await resolve_run_home(ctx.deps)
        assert before is not None and before.default.path == "/home/agent/projects/proj-1"

        result = await _open_worktree(ctx)

        assert switch.opened == ["conv-a"]
        assert ctx.deps.worktree
        after = await run_workspace(ctx)
        assert after is not None
        assert after.kind == "worktree"
        assert after.root != root
        assert after.branch == "heph/conv-a"
        assert "heph/conv-a" in result and "/home/agent/worktrees/proj-1" in result
        home = await resolve_run_home(ctx.deps)
        assert home is not None
        # Relative paths land in the worktree, and the operator's folder is theirs again.
        assert home.default.path == "/home/agent/worktrees/proj-1"
        checkout = home.place_of("~/projects/proj-1/hello.txt")
        assert checkout is not None and not home.writable(checkout)

    async def test_an_unfiled_chat_has_nothing_to_open(self, tmp_path: Path):
        root = await _repo(tmp_path / "project")
        switch = _FakeSwitch()
        ctx = _ctx(_caps(tmp_path, root, switch), project_id=None)
        assert "not filed under a project" in await _open_worktree(ctx)
        assert switch.opened == []
        assert not ctx.deps.worktree

    async def test_a_delegated_agent_cannot_move_its_parents_workspace(self, tmp_path: Path):
        root = await _repo(tmp_path / "project")
        switch = _FakeSwitch()
        ctx = _ctx(_caps(tmp_path, root, switch), conversation_id="child", workspace_key="conv-a")
        assert "delegated" in await _open_worktree(ctx)
        assert switch.opened == []

    async def test_a_worktree_chat_is_told_so(self, tmp_path: Path):
        root = await _repo(tmp_path / "project")
        switch = _FakeSwitch()
        ctx = _ctx(_caps(tmp_path, root, switch), worktree=True)
        assert "already" in await _open_worktree(ctx)
        assert switch.opened == []


class TestTheStoredBinding:
    async def _store(self, tmp_path: Path) -> ConversationStore:
        engine = make_engine("sqlite:///:memory:")
        init_db(engine)
        vault = Vault(tmp_path / "keyfile.json")
        await vault.setup("pw")
        return ConversationStore(engine, vault)

    async def test_a_thread_keeps_its_worktree_until_it_is_closed(self, tmp_path: Path):
        store = await self._store(tmp_path)
        conversation_id = await store.create_conversation(OWNER, project_id="p1")
        assert not (await store.binding(conversation_id)).worktree
        assert await store.open_worktree(conversation_id)
        binding = await store.binding(conversation_id)
        assert binding.worktree and binding.project_id == "p1"
        await store.close_worktree(conversation_id)
        assert not (await store.binding(conversation_id)).worktree

    async def test_an_unfiled_thread_has_no_worktree_to_open(self, tmp_path: Path):
        store = await self._store(tmp_path)
        conversation_id = await store.create_conversation(OWNER)
        assert not await store.open_worktree(conversation_id)
        assert not (await store.binding(conversation_id)).worktree

    async def test_a_spawned_agent_works_in_its_parents_worktree_and_leases(
        self, tmp_path: Path
    ):
        store = await self._store(tmp_path)
        parent = await store.create_conversation(OWNER, project_id="p1")
        _ = await store.open_worktree(parent)

        async def child(key: str) -> tuple[bool, str | None]:
            conversation_id = await store.create_conversation(
                OWNER, project_id="p1", parent_id=parent, workspace_key=key
            )
            binding = await store.binding(conversation_id)
            return binding.worktree, binding.leases_from

        # Sharing its parent's files, it works under the parent's leases; working apart, in
        # a fork or a workspace of its own, under none.
        assert await child(parent) == (True, parent)
        assert await child(f"{parent}/s-1") == (True, "")
        assert await child("") == (False, "")
        assert (await store.binding(parent)).leases_from is None
