"""Shared setup for tests that drive the `shell` tools against a project folder.

Commands run in a platform VM whose guest is this machine (`vm_local`), so the real guest
scripts and the real project sync are in play; tests that use it skip without root and
bubblewrap.
"""

from __future__ import annotations

import subprocess
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

from pydantic import JsonValue, TypeAdapter
from pydantic_ai import RunContext
from pydantic_ai.models.test import TestModel
from pydantic_ai.toolsets import AbstractToolset
from pydantic_ai.toolsets.abstract import ToolsetTool
from pydantic_ai.usage import RunUsage

from core.container import ServiceContainer
from runs import Run, RunStream
from services.egress import EgressPolicy
from services.projects import ProjectStore
from services.projects.checkout import CheckoutHolders
from services.vm.project import ProjectCopies
from services.workspace_kind import WorkspaceKind
from tools.deps import RunDeps

from .vm_local import local_vm

OWNER = "operator"
_JSON = TypeAdapter(JsonValue)


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def make_repo(root: Path) -> Path:
    root.mkdir(parents=True)
    _ = git(root, "init", "-q", "-b", "main")
    _ = git(root, "config", "user.name", "Operator")
    _ = git(root, "config", "user.email", "op@example.com")
    _ = (root / "hello.txt").write_text("original\n")
    _ = git(root, "add", ".")
    _ = git(root, "commit", "-q", "-m", "first")
    return root


class _View:
    def __init__(self, root_path: str) -> None:
        self.root_path: str = root_path
        self.base_ref: str = "main"


class _Projects:
    def __init__(self, root: Path) -> None:
        self._root: Path = root

    async def get(self, _owner_id: str, _project_id: str) -> _View:
        return _View(str(self._root))


@asynccontextmanager
async def local_copies(tmp_path: Path) -> AsyncGenerator[ProjectCopies]:
    """A :class:`ProjectCopies` over a VM running on this machine."""
    platform, _driver = await local_vm(tmp_path)
    copies = ProjectCopies(platform, idle_ttl_s=3600, reap_interval_s=3600)
    try:
        yield copies
    finally:
        await copies.stop()
        await platform.stop()


def checkout_ctx(
    root: Path,
    *,
    copies: ProjectCopies | None = None,
    egress: EgressPolicy | None = None,
    kind: WorkspaceKind = "checkout",
    project_id: str = "proj-1",
    approved: bool = True,
) -> RunContext[RunDeps]:
    """A run in the project folder at ``root``, with the given VM and egress policy."""
    caps = ServiceContainer()
    caps.add(_Projects(root), as_type=ProjectStore)
    caps.add(CheckoutHolders())
    if copies is not None:
        caps.add(copies, as_type=ProjectCopies)
    if egress is not None:
        caps.add(egress, as_type=EgressPolicy)
    run = Run(id="run-1", kind="chat", owner_id=OWNER, stream=RunStream())
    ctx = RunContext(
        deps=RunDeps(
            run=run,
            owner_id=OWNER,
            caps=caps,
            conversation_id="conv-a",
            project_id=project_id,
            workspace_kind=kind,
        ),
        model=TestModel(),
        usage=RunUsage(),
    )
    # Approval gates the first command; `tool_call_approved` is what the engine sets on the
    # re-invocation, so the post-approval call is driven directly.
    ctx.tool_call_approved = approved
    return ctx


class Shell:
    """The four tools, driven the way the engine drives them.

    The result comes back as the tool returned it: a dict when a command ran, a string when
    the guard refused before anything was spawned.
    """

    def __init__(self, toolset: AbstractToolset[RunDeps], ctx: RunContext[RunDeps]) -> None:
        self._toolset: AbstractToolset[RunDeps] = toolset
        self.ctx: RunContext[RunDeps] = ctx
        self._tools: dict[str, ToolsetTool[RunDeps]] | None = None

    async def call(self, name: str, **args: object) -> JsonValue:
        tools = self._tools
        if tools is None:
            tools = self._tools = await self._toolset.get_tools(self.ctx)
        return _JSON.validate_python(
            await self._toolset.call_tool(name, args, self.ctx, tools[name])
        )
