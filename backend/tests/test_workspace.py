"""One workspace per run, resolved once.

The bug this file exists to catch is not "the file tools point at the wrong directory" —
that would be obvious. It is the agent **editing a file it cannot run**: file tools rooted
at a worktree while attachments stage into a container, or a skill's scripts announced at
`/work/...` in a conversation whose shell runs on the host. Every subsystem that used to
assume "the workspace is the sandbox" has to come out of the same resolver, so the tests
below assert the *agreement* between them rather than each one in isolation.

The other assertion worth naming: **rooting, not isolation**. Two conversations having
different `Shell` state is satisfied by the library's own `for_run` with both of them
rooted at the same path — which is exactly the bug. So the roots are compared to the
worktrees they should be, not merely to each other.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import final

import pytest
from pydantic_ai import RunContext
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage
from pydantic_ai.workspaces import Workspace

from core.container import ServiceContainer
from runs import Run, RunStream
from services.home.scratch import HostFiles, ScratchDirs
from services.projects import ProjectStore, WorktreeManager
from services.sandbox import SandboxError
from services.workspace import (
    SANDBOX_MOUNT,
    WORKTREE_SCRATCH,
    RunWorkspace,
    resolve_workspace,
)
from tools.deps import RunDeps
from tools.home_workspace import home_workspace
from tools.workspace import run_workspace

OWNER = "operator"


# --- fakes: the project capability the resolver reaches for --------------------------


@final
@dataclass(frozen=True)
class _View:
    root_path: str
    base_ref: str = "main"


@final
class _FakeProjects:
    def __init__(self, roots: dict[str, Path]) -> None:
        self._roots: dict[str, Path] = roots

    async def get(self, _owner_id: str, project_id: str) -> _View:
        return _View(root_path=str(self._roots[project_id]))

    async def slugs(self, _owner_id: str) -> dict[str, str]:
        return {project_id: project_id for project_id in self._roots}

    async def workspace_roots(self, _owner_id: str) -> dict[str, Path]:
        return dict(self._roots)


async def _git(cwd: Path, *args: str) -> None:
    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=str(cwd),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    _ = await proc.communicate()


async def _repo(root: Path) -> Path:
    root.mkdir(parents=True)
    _ = (root / "hello.txt").write_text("original\n")
    await _git(root, "git", "init", "-b", "main")
    await _git(root, "git", "add", "-A")
    await _git(root, "git", "-c", "user.name=T", "-c", "user.email=t@e", "commit", "-m", "first")
    return root


def _caps(tmp_path: Path, roots: dict[str, Path] | None = None) -> ServiceContainer:
    caps = ServiceContainer()
    caps.add(ScratchDirs(tmp_path / "sandbox"))
    if roots is not None:
        caps.add(_FakeProjects(roots), as_type=ProjectStore)
        caps.add(WorktreeManager(tmp_path / "worktrees"), as_type=WorktreeManager)
    return caps


def _ctx(
    caps: ServiceContainer,
    *,
    run_id: str = "run-1",
    conversation_id: str | None = None,
    project_id: str | None = None,
    worktree: bool = False,
) -> RunContext[RunDeps]:
    run = Run(id=run_id, kind="chat", owner_id=OWNER, stream=RunStream())
    deps = RunDeps(
        run=run,
        owner_id=OWNER,
        caps=caps,
        conversation_id=conversation_id,
        project_id=project_id,
        worktree=worktree,
    )
    return RunContext(
        deps=deps,
        workspace=Workspace(home_workspace(deps)),
        model=TestModel(),
        usage=RunUsage(),
    )


# --- sandbox: unchanged, and the /work translation is the resolver's --------------


class TestSandbox:
    async def test_resolves_the_conversations_own_sandbox_workspace(self, tmp_path: Path):
        ctx = _ctx(_caps(tmp_path), conversation_id="conv-a")
        workspace = await run_workspace(ctx)
        assert workspace is not None
        assert workspace.kind == "sandbox"
        assert workspace.root == ScratchDirs(tmp_path / "sandbox").path("conv-a")

    async def test_two_conversations_get_different_roots(self, tmp_path: Path):
        caps = _caps(tmp_path)
        a = await run_workspace(_ctx(caps, conversation_id="conv-a", run_id="r1"))
        b = await run_workspace(_ctx(caps, conversation_id="conv-b", run_id="r2"))
        assert a is not None and b is not None
        # Rooting, not isolation: named directories, not merely distinct objects.
        scratch = ScratchDirs(tmp_path / "sandbox")
        assert a.root == scratch.path("conv-a")
        assert b.root == scratch.path("conv-b")

    async def test_the_model_is_told_the_mount_path_not_the_host_path(self, tmp_path: Path):
        ctx = _ctx(_caps(tmp_path), conversation_id="conv-a")
        workspace = await run_workspace(ctx)
        assert workspace is not None
        # The host path means nothing on the other side of the bind mount.
        assert workspace.display("attachments/x.pdf") == f"{SANDBOX_MOUNT}/attachments/x.pdf"
        assert workspace.stage_prefix == ""

    async def test_no_sandbox_means_no_workspace(self):
        ctx = _ctx(ServiceContainer(), conversation_id="conv-a")
        assert await run_workspace(ctx) is None

    async def test_it_is_resolved_once_per_run(self, tmp_path: Path):
        ctx = _ctx(_caps(tmp_path), conversation_id="conv-a")
        first = await run_workspace(ctx)
        second = await run_workspace(ctx)
        # A code turn's first resolution runs `git worktree add`; a turn makes many
        # file-tool calls, and it must not run once per call.
        assert first is second


# --- worktree: the worktree, and the one-workspace invariant ------------------------


class TestWorktree:
    async def _ctx(
        self, tmp_path: Path, *, conversation_id: str = "conv-a", run_id: str = "run-1"
    ) -> tuple[Path, RunContext[RunDeps]]:
        root = await _repo(tmp_path / "project")
        caps = _caps(tmp_path, {"proj-1": root})
        return root, _ctx(
            caps,
            conversation_id=conversation_id,
            run_id=run_id,
            project_id="proj-1",
            worktree=True,
        )

    async def test_resolves_the_projects_worktree_not_the_operators_tree(self, tmp_path: Path):
        root, ctx = await self._ctx(tmp_path)
        workspace = await run_workspace(ctx)
        assert workspace is not None
        assert workspace.kind == "worktree"
        assert workspace.branch == "heph/conv-a"
        # The assertion the whole design exists for: never the operator's own checkout.
        assert workspace.root != root
        assert (workspace.root / "hello.txt").read_text() == "original\n"

    async def test_the_model_is_told_the_worktrees_path_in_its_home(self, tmp_path: Path):
        _root, ctx = await self._ctx(tmp_path)
        workspace = await run_workspace(ctx)
        assert workspace is not None
        # The worktree's folder in the home, which the file tools and the shell both name
        # it by — never the host path neither of them can open, nor the scratch's `/work`.
        shown = workspace.display("src/main.py")
        assert shown == "/home/agent/worktrees/proj-1/src/main.py"
        assert not shown.startswith(SANDBOX_MOUNT)

    async def test_staged_files_land_inside_the_worktree_and_out_of_the_diff(self, tmp_path: Path):
        _root, ctx = await self._ctx(tmp_path)
        workspace = await run_workspace(ctx)
        assert workspace is not None
        # Inside, or the file tools cannot reach what was staged...
        assert workspace.stage_prefix == f"{WORKTREE_SCRATCH}/"
        workspace.files.write_file(f"{workspace.stage_prefix}attachments/a.txt", b"hi")
        assert (workspace.root / WORKTREE_SCRATCH / "attachments" / "a.txt").is_file()
        # ...and ignored, or the operator's review fills up with their own attachments.
        code, out = await _status(workspace.root)
        assert code == 0
        assert WORKTREE_SCRATCH not in out

    async def test_a_worktree_of_a_missing_project_has_no_workspace_not_a_sandbox(
        self, tmp_path: Path
    ):
        # Falling back to the sandbox would be the worst answer: the agent would edit
        # files in a container it believes is the worktree, and nothing would say why.
        caps = _caps(tmp_path, {})
        ctx = _ctx(caps, conversation_id="c", worktree=True, project_id="gone", run_id="r2")
        assert await run_workspace(ctx) is None


async def _status(cwd: Path) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        "git",
        "status",
        "--porcelain",
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate()
    return proc.returncode or 0, out.decode()


# --- the host-side staging adapter ---------------------------------------------------


class TestHostFiles:
    def test_a_write_outside_the_root_is_refused(self, tmp_path: Path):
        # A staged filename is operator content; containment is the sandbox's own check,
        # reused rather than re-derived.
        files = HostFiles(tmp_path)
        with pytest.raises(SandboxError):
            files.write_file("../escaped.txt", b"x")

    def test_round_trips_within_the_root(self, tmp_path: Path):
        files = HostFiles(tmp_path)
        files.write_file("nested/a.txt", b"hi")
        assert files.read_file("nested/a.txt") == b"hi"


# --- the resolver's own contract -----------------------------------------------------


class TestResolveWorkspace:
    async def test_no_handles_at_all_is_no_workspace(self):
        assert (
            await resolve_workspace(
                project_id=None,
                worktree=False,
                conversation_id="c",
                workspace_key="c",
                owner_id=OWNER,
                scratch=None,
                projects=None,
                worktrees=None,
            )
            is None
        )

    def test_display_never_doubles_a_separator(self, tmp_path: Path):
        workspace = RunWorkspace(root=tmp_path, kind="sandbox", files=HostFiles(tmp_path))
        assert workspace.display("/attachments/a") == f"{SANDBOX_MOUNT}/attachments/a"
