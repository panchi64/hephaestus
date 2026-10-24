"""A sub-agent spawned into a worktree of its own: its parent sees the worktree, and lands
or drops its work when it judges it ready. Against real git."""

from __future__ import annotations

from pathlib import Path
from typing import override

import pytest
from pydantic import JsonValue
from pydantic_ai import ModelRetry, RunContext
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage

from core.container import ServiceContainer
from core.db import init_db, make_engine
from core.vault import Vault
from runs import Run, RunStream
from services.agents import AgentDefinition
from services.home.changes import ThreadChanges
from services.home.paths import Place
from services.projects.repo import WorktreeState, run_git, run_git_ok
from services.projects.store import ProjectStore
from services.projects.worktree import WorktreeManager
from services.settings_store import SettingsStore
from services.subagents import (
    ChildWorktree,
    FiledReport,
    LaunchedSubagent,
    Recipient,
    SubagentLauncher,
    SubagentParent,
    SubagentView,
)
from tools.agents import agents_toolset
from tools.child_worktrees import discard_child, merge_child
from tools.deps import RunDeps
from tools.home import resolve_run_home

from ._json import JSON, obj

OWNER = "operator"
PARENT = "parent"


class _Launcher(SubagentLauncher):
    """One spawned agent, `fixer`, in the worktree it was cut into."""

    def __init__(self, child: ChildWorktree | None, *, status: str = "done") -> None:
        self.child: ChildWorktree | None = child
        self.status: str = status
        self.launched: list[tuple[str, bool]] = []

    @override
    async def launch(
        self,
        owner_id: str,
        agent: AgentDefinition,
        task: str,
        *,
        handle: str,
        parent: SubagentParent | None = None,
        worktree: bool = False,
    ) -> LaunchedSubagent:
        self.launched.append((agent.name, worktree))
        return LaunchedSubagent("s1", "child", "r1", agent.name, handle, task)

    @override
    async def recipient(self, owner_id: str, conversation_id: str, to: str) -> Recipient:
        raise NotImplementedError

    @override
    async def message(
        self,
        owner_id: str,
        conversation_id: str,
        to: str,
        text: str,
        *,
        ledger_prompt_message_id: str | None = None,
        ledger_thread: str | None = None,
    ) -> str:
        raise NotImplementedError

    @override
    async def file_report(
        self, owner_id: str, conversation_id: str, run_id: str, report: FiledReport
    ) -> str:
        raise NotImplementedError

    @override
    async def read(self, owner_id: str, conversation_id: str, handle: str) -> SubagentView:
        return SubagentView("s1", "child", "r1", "worker", handle, "fix it", self.status)

    @override
    async def stop(self, owner_id: str, conversation_id: str, handle: str) -> list[str]:
        raise NotImplementedError

    @override
    async def tree(self, owner_id: str, conversation_id: str) -> list[SubagentView]:
        return []

    @override
    async def below(self, owner_id: str, conversation_id: str) -> list[SubagentView]:
        return []

    @override
    async def awaiting_reports(self, owner_id: str, conversation_id: str) -> bool:
        return False

    @override
    async def worktrees(self, owner_id: str, conversation_id: str) -> list[ChildWorktree]:
        child = self.child
        return [child] if child is not None and child.path.exists() else []

    @override
    async def worktree(self, owner_id: str, conversation_id: str, handle: str) -> ChildWorktree:
        assert self.child is not None
        return self.child


class _World:
    def __init__(
        self,
        root: Path,
        project_id: str,
        caps: ServiceContainer,
        worktrees: WorktreeManager,
        changes: ThreadChanges,
    ) -> None:
        self.root: Path = root
        self.project_id: str = project_id
        self.caps: ServiceContainer = caps
        self.worktrees: WorktreeManager = worktrees
        self.changes: ThreadChanges = changes

    def ctx(self, *, worktree: bool = False) -> RunContext[RunDeps]:
        run = Run(id="run-parent", kind="chat", owner_id=OWNER, stream=RunStream())
        deps = RunDeps(
            run=run,
            owner_id=OWNER,
            caps=self.caps,
            conversation_id=PARENT,
            project_id=self.project_id,
            worktree=worktree,
        )
        return RunContext(deps=deps, model=TestModel(), usage=RunUsage())

    async def cut(self, source: WorktreeState | None, *, status: str = "done") -> ChildWorktree:
        state = await self.worktrees.cut_child(
            project_id=self.project_id,
            root=self.root,
            conversation_id=PARENT,
            delegation_id="d1",
            source=source,
        )
        child = ChildWorktree(
            handle="fixer",
            conversation_id="child",
            project_id=self.project_id,
            path=state.path,
            branch=state.branch,
            base_ref=state.base_ref,
            holder_id=PARENT,
            delegation_id="d1",
        )
        self.caps.add(_Launcher(child, status=status), as_type=SubagentLauncher)
        return child


async def _world(tmp_path: Path) -> _World:
    root = tmp_path / "api"
    root.mkdir()
    _ = (root / "hello.txt").write_text("original\n")
    for argv in (
        ("init", "-b", "main"),
        ("add", "-A"),
        ("-c", "user.name=T", "-c", "user.email=t@e", "commit", "-m", "first"),
    ):
        _ = await run_git_ok(root, *argv)
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    vault = Vault(tmp_path / "keyfile.json")
    await vault.setup("pw")
    projects = ProjectStore(engine, vault, SettingsStore(engine))
    project = await projects.create(OWNER, "api", str(root))
    worktrees = WorktreeManager(tmp_path / "worktrees")
    changes = ThreadChanges(engine, vault)
    caps = ServiceContainer()
    caps.add(projects)
    caps.add(worktrees)
    caps.add(changes)
    return _World(root, project.id, caps, worktrees, changes)


async def test_the_parent_sees_the_childs_worktree_in_its_home(tmp_path: Path):
    world = await _world(tmp_path)
    child = await world.cut(None)

    home = await resolve_run_home(world.ctx().deps)

    assert home is not None
    mount = home.mount(Place("worktrees", "api-fixer"))
    assert mount is not None and mount.root == child.path and mount.writable


async def test_merging_into_the_checkout_lands_the_work_and_can_be_put_back(tmp_path: Path):
    world = await _world(tmp_path)
    child = await world.cut(None)
    _ = (child.path / "added.txt").write_text("the sub-agent's work\n")

    result = await merge_child(world.ctx(), "fixer")

    assert result["merged"] is True
    assert result["files"] == ["added.txt"]
    assert (world.root / "added.txt").read_text() == "the sub-agent's work\n"
    assert not child.path.exists()
    listed = {view.path: view.status for view in await world.changes.list(OWNER, PARENT)}
    assert listed == {"added.txt": "added"}
    _ = await world.changes.revert(OWNER, PARENT, {world.project_id: world.root})
    assert not (world.root / "added.txt").exists()


async def test_merging_into_the_parents_worktree_leaves_the_checkout_alone(tmp_path: Path):
    world = await _world(tmp_path)
    parent = await world.worktrees.acquire(
        project_id=world.project_id, root=world.root, base_ref="main", conversation_id=PARENT
    )
    child = await world.cut(parent)
    _ = (child.path / "added.txt").write_text("the sub-agent's work\n")

    result = await merge_child(world.ctx(worktree=True), "fixer")

    assert result["merged"] is True
    assert (parent.path / "added.txt").read_text() == "the sub-agent's work\n"
    assert not (world.root / "added.txt").exists()


async def test_a_clash_changes_nothing_and_keeps_the_child(tmp_path: Path):
    world = await _world(tmp_path)
    child = await world.cut(None)
    _ = (child.path / "hello.txt").write_text("the sub-agent's version\n")
    _ = (world.root / "hello.txt").write_text("the operator's version\n")

    result = await merge_child(world.ctx(), "fixer")

    assert result["merged"] is False
    assert result["conflicts"] == ["hello.txt"]
    assert "help:" in str(result["detail"])
    assert (world.root / "hello.txt").read_text() == "the operator's version\n"
    assert child.path.exists()


async def test_a_child_still_working_is_not_merged(tmp_path: Path):
    world = await _world(tmp_path)
    _ = await world.cut(None, status="running")

    with pytest.raises(ModelRetry, match="still working"):
        _ = await merge_child(world.ctx(), "fixer")


async def test_discarding_drops_the_worktree_and_its_branch(tmp_path: Path):
    world = await _world(tmp_path)
    child = await world.cut(None)

    result = await discard_child(world.ctx(), "fixer")

    assert result["discarded"] is True
    assert not child.path.exists()
    code, _, _ = await run_git(world.root, "rev-parse", "--verify", child.branch)
    assert code != 0


async def _spawn(world: _World, task: str, **extra: JsonValue) -> dict[str, JsonValue]:
    ctx = world.ctx()
    toolset = agents_toolset()
    tools = await toolset.get_tools(ctx)
    args: dict[str, object] = {"name": "fixer", "agent_name": "worker", "task": task, **extra}
    return obj(JSON.validate_python(await toolset.call_tool("spawn", args, ctx, tools["spawn"])))


async def test_a_writing_agent_is_spawned_into_a_worktree_without_asking(tmp_path: Path):
    world = await _world(tmp_path)
    launcher = _Launcher(None)
    world.caps.add(launcher, as_type=SubagentLauncher)
    task = (
        "Fix the greeting in hello.txt so it says hello, starting from the file itself, "
        + "and report the diff when the file reads correctly."
    )

    result = await _spawn(world, task, worktree=True)

    assert result["spawned"] is True
    assert launcher.launched == [("worker", True)]


async def test_a_task_too_short_to_work_from_is_sent_back(tmp_path: Path):
    world = await _world(tmp_path)
    world.caps.add(_Launcher(None), as_type=SubagentLauncher)

    with pytest.raises(ModelRetry) as raised:
        _ = await _spawn(world, "fix it")

    assert "help:" in str(raised.value)


async def test_the_child_works_in_its_worktree_and_leaves_the_checkout_alone(tmp_path: Path):
    world = await _world(tmp_path)
    child = await world.cut(None)
    run = Run(id="run-child", kind="linked", owner_id=OWNER, stream=RunStream())
    deps = RunDeps(
        run=run,
        owner_id=OWNER,
        caps=world.caps,
        conversation_id="child",
        project_id=world.project_id,
        worktree=True,
        leases_from=PARENT,
        workspace_key=f"{PARENT}/d1",
    )

    home = await resolve_run_home(deps)

    assert home is not None
    assert home.default == Place("worktrees", "api")
    worktree = home.mount(Place("worktrees", "api"))
    assert worktree is not None and worktree.root == child.path and worktree.writable
    assert not home.writable(Place("projects", "api"))
