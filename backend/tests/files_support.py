"""A home with one project, ``api``, and the files toolset called against it."""

from __future__ import annotations

from pathlib import Path
from typing import cast

from pydantic_ai import RunContext
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage
from pydantic_ai.workspaces import Workspace

from core.container import ServiceContainer
from core.db import init_db, make_engine
from core.vault import Vault
from runs import Run, RunStream
from services.home.leases import WriteLeases
from services.home.scratch import ScratchDirs
from services.projects.store import ProjectStore
from services.settings_store import SettingsStore
from tools import RunDeps, build_agent_toolsets, harness_events_capability
from tools.files import files_toolset
from tools.home_workspace import home_workspace

_TOOLSET = build_agent_toolsets({"files": files_toolset()})[0]
README = "~/projects/api/README.md"


async def project_caps(tmp_path: Path) -> tuple[ServiceContainer, Path]:
    """Services for a home holding the ``api`` project, and the project's root."""
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    vault = Vault(tmp_path / "keyfile.json")
    await vault.setup("pw")
    projects = ProjectStore(engine, vault, SettingsStore(engine))
    root = tmp_path / "api"
    root.mkdir()
    _ = (root / "README.md").write_text("the api\n")
    _ = await projects.create("operator", "api", str(root))
    caps = ServiceContainer()
    caps.add(projects)
    caps.add(WriteLeases(engine, ttl_s=3600))
    caps.add(ScratchDirs(tmp_path / "scratch"))
    return caps, root


def run_deps(caps: ServiceContainer, conversation: str = "conv-1") -> RunDeps:
    run = Run(id="t", kind="chat", owner_id="operator", stream=RunStream())
    return RunDeps(run=run, owner_id="operator", caps=caps, conversation_id=conversation)


async def call_files(
    caps: ServiceContainer,
    tool: str,
    args: dict[str, object],
    *,
    conversation: str = "conv-1",
    approved: bool = True,
    messages: list[ModelMessage] | None = None,
) -> str:
    """One files tool call, as a turn on ``conversation`` would make it."""
    deps = run_deps(caps, conversation)
    ctx = RunContext(
        deps=deps,
        workspace=Workspace(home_workspace(deps)),
        messages=messages or [],
        model=TestModel(),
        usage=RunUsage(),
        _event_stream_buffer=[],
        _capability=harness_events_capability(),
    )
    ctx.tool_call_approved = approved
    tools = await _TOOLSET.get_tools(ctx)
    name = f"files_{tool}"
    return str(cast(object, await _TOOLSET.call_tool(name, args, ctx, tools[name])))
