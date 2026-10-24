"""A home with one project, ``api``, and the files toolset called against it."""

from __future__ import annotations

from pathlib import Path
from typing import cast

from pydantic_ai import RunContext
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage

from core.container import ServiceContainer
from core.db import init_db, make_engine
from core.vault import Vault
from runs import Run, RunStream
from services.home.leases import WriteLeases
from services.projects.store import ProjectStore
from services.sandbox import SandboxSessionManager
from services.settings_store import SettingsStore
from tools import RunDeps, build_agent_toolsets, harness_events_capability
from tools.files import files_toolset

_TOOLSET = build_agent_toolsets({"files": files_toolset()})[0]
README = "~/projects/api/README.md"


class _Session:
    def __init__(self, workspace: Path) -> None:
        self._workspace: Path = workspace

    def ensure_workspace(self) -> Path:
        self._workspace.mkdir(parents=True, exist_ok=True)
        return self._workspace


class _Manager:
    """Hands out one session per key, each with its own scratch directory."""

    def __init__(self, root: Path) -> None:
        self._root: Path = root

    async def acquire(self, key: str, *, holder: object = None) -> _Session:
        del holder
        return _Session(self._root / "scratch" / key)


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
    caps.add(_Manager(tmp_path), as_type=SandboxSessionManager)
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
    ctx = RunContext(
        deps=run_deps(caps, conversation),
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
