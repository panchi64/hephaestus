"""The project tools name a project by its path in the agent's home, which the file and
shell tools take as written, never by its folder on the operator's machine."""

from __future__ import annotations

from pathlib import Path

from pydantic import JsonValue
from pydantic_ai import RunContext
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage

from core.container import ServiceContainer
from core.db import init_db, make_engine
from core.vault import Vault
from runs import Run, RunStream
from services.projects.store import ProjectStore
from services.settings_store import SettingsStore
from tools.deps import RunDeps
from tools.projects import project_toolset

from ._json import JSON, arr, obj

OWNER = "operator"


async def _call(tmp_path: Path, tool: str) -> dict[str, JsonValue]:
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    vault = Vault(tmp_path / "keyfile.json")
    await vault.setup("pw")
    projects = ProjectStore(engine, vault, SettingsStore(engine))
    root = tmp_path / "Pantry App"
    root.mkdir()
    project = await projects.create(OWNER, "Pantry App", str(root))
    caps = ServiceContainer()
    caps.add(projects)
    run = Run(id="run", kind="chat", owner_id=OWNER, stream=RunStream())
    deps = RunDeps(run=run, owner_id=OWNER, caps=caps, project_id=project.id, worktree=True)
    ctx = RunContext(deps=deps, model=TestModel(), usage=RunUsage())
    toolset = project_toolset()
    tools = await toolset.get_tools(ctx)
    return obj(JSON.validate_python(await toolset.call_tool(tool, {}, ctx, tools[tool])))


async def test_the_listing_gives_each_project_its_home_path(tmp_path: Path):
    listed = await _call(tmp_path, "list")
    project = obj(arr(listed["projects"])[0])
    assert project["path"] == "/home/agent/projects/pantry-app"


async def test_the_active_project_gives_its_home_and_worktree_paths(tmp_path: Path):
    active = obj((await _call(tmp_path, "active"))["project"])
    assert active["path"] == "/home/agent/projects/pantry-app"
    assert active["worktree_path"] == "/home/agent/worktrees/pantry-app"
