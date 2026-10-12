"""Shared setup for tests that drive the `shell` tools against a project folder.

Commands run in a platform VM whose guest is this machine (`vm_local`), so the real guest
scripts and the real project sync are in play; tests that use it skip without root and
bubblewrap.
"""

from __future__ import annotations

import subprocess
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage

from core.container import ServiceContainer
from runs import Run, RunStream
from services.egress import EgressPolicy
from services.projects import ProjectStore
from services.projects.checkout import CheckoutHolders
from services.vm.project import ProjectCopies
from tools.deps import RunDeps

from .vm_local import local_vm

OWNER = "operator"


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def make_repo(root: Path) -> Path:
    root.mkdir(parents=True)
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.name", "Operator")
    git(root, "config", "user.email", "op@example.com")
    (root / "hello.txt").write_text("original\n")
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "first")
    return root


class _Projects:
    def __init__(self, root: Path) -> None:
        self._root = root

    async def get(self, owner_id: str, project_id: str):
        class _View:
            root_path = str(self._root)
            base_ref = "main"

        return _View()


@asynccontextmanager
async def local_copies(tmp_path: Path) -> AsyncIterator[ProjectCopies]:
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
    copies: Any = None,
    egress: EgressPolicy | None = None,
    kind: str = "checkout",
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

    def __init__(self, toolset, ctx: RunContext[RunDeps]) -> None:
        self._toolset = toolset
        self.ctx = ctx
        self._tools: dict | None = None

    async def call(self, name: str, **args):
        if self._tools is None:
            self._tools = await self._toolset.get_tools(self.ctx)
        return await self._toolset.call_tool(name, args, self.ctx, self._tools[name])
