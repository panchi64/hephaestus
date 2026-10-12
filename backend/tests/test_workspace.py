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
from pathlib import Path

import pytest
from pydantic_ai import ModelRetry, RunContext
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage

from core.container import ServiceContainer
from runs import Run, RunStream
from services.projects import ProjectStore, WorktreeManager
from services.projects.checkout import CheckoutHolders
from services.sandbox import SandboxError, SandboxSessionManager
from services.vm.project import ProjectCopies
from services.workspace import (
    SANDBOX_MOUNT,
    WORKTREE_SCRATCH,
    HostFiles,
    RunWorkspace,
    resolve_workspace,
)
from tools.deps import RunDeps
from tools.shell import shell_toolset
from tools.workspace import run_workspace

from .shell_support import Shell, checkout_ctx, local_copies, make_repo
from .vm_local import can_confine

OWNER = "operator"


# --- fakes: the two capabilities the resolver reaches for ----------------------------


class _FakeSession:
    def __init__(self, path: Path) -> None:
        self._path = path

    def ensure_workspace(self) -> Path:
        return self._path

    def read_file(self, relpath: str) -> bytes:
        return (self._path / relpath).read_bytes()

    def write_file(self, relpath: str, content: bytes) -> None:
        target = self._path / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)


class _FakeSessions:
    """One workspace directory per conversation key, like the real manager."""

    def __init__(self, root: Path) -> None:
        self._root = root
        self.holders: dict[str, object] = {}

    async def acquire(self, key: str, *, holder: object = None) -> _FakeSession:
        path = self._root / key
        path.mkdir(parents=True, exist_ok=True)
        if holder is not None:
            self.holders[key] = holder
        return _FakeSession(path)


class _FakeProjects:
    def __init__(self, roots: dict[str, Path]) -> None:
        self._roots = roots

    async def get(self, owner_id: str, project_id: str):
        class _View:
            root_path = str(self._roots[project_id])
            base_ref = "main"

        return _View()


async def _git(cwd: Path, *args: str) -> None:
    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=str(cwd),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.communicate()


async def _repo(root: Path) -> Path:
    root.mkdir(parents=True)
    (root / "hello.txt").write_text("original\n")
    await _git(root, "git", "init", "-b", "main")
    await _git(root, "git", "add", "-A")
    await _git(root, "git", "-c", "user.name=T", "-c", "user.email=t@e", "commit", "-m", "first")
    return root


def _caps(tmp_path: Path, roots: dict[str, Path] | None = None) -> ServiceContainer:
    caps = ServiceContainer()
    caps.add(_FakeSessions(tmp_path / "sandbox"), as_type=SandboxSessionManager)
    if roots is not None:
        caps.add(_FakeProjects(roots), as_type=ProjectStore)
        caps.add(WorktreeManager(tmp_path / "worktrees"), as_type=WorktreeManager)
        caps.add(CheckoutHolders())
    return caps


def _ctx(caps: ServiceContainer, **deps) -> RunContext[RunDeps]:
    run = Run(id=deps.pop("run_id", "run-1"), kind="chat", owner_id=OWNER, stream=RunStream())
    return RunContext(
        deps=RunDeps(run=run, owner_id=OWNER, caps=caps, **deps),
        model=TestModel(),
        usage=RunUsage(),
    )


# --- sandbox: unchanged, and the /work translation is the resolver's --------------


class TestSandbox:
    async def test_resolves_the_conversations_own_sandbox_workspace(self, tmp_path):
        ctx = _ctx(_caps(tmp_path), conversation_id="conv-a")
        workspace = await run_workspace(ctx)
        assert workspace is not None
        assert workspace.kind == "sandbox"
        assert workspace.root == tmp_path / "sandbox" / "conv-a"

    async def test_two_conversations_get_different_roots(self, tmp_path):
        caps = _caps(tmp_path)
        a = await run_workspace(_ctx(caps, conversation_id="conv-a", run_id="r1"))
        b = await run_workspace(_ctx(caps, conversation_id="conv-b", run_id="r2"))
        assert a is not None and b is not None
        # Rooting, not isolation: named directories, not merely distinct objects.
        assert a.root == tmp_path / "sandbox" / "conv-a"
        assert b.root == tmp_path / "sandbox" / "conv-b"

    async def test_the_model_is_told_the_mount_path_not_the_host_path(self, tmp_path):
        ctx = _ctx(_caps(tmp_path), conversation_id="conv-a")
        workspace = await run_workspace(ctx)
        assert workspace is not None
        # The host path means nothing on the other side of the bind mount.
        assert workspace.display("attachments/x.pdf") == f"{SANDBOX_MOUNT}/attachments/x.pdf"
        assert workspace.stage_prefix == ""

    async def test_no_sandbox_means_no_workspace(self, tmp_path):
        ctx = _ctx(ServiceContainer(), conversation_id="conv-a")
        assert await run_workspace(ctx) is None

    async def test_it_is_resolved_once_per_run(self, tmp_path):
        ctx = _ctx(_caps(tmp_path), conversation_id="conv-a")
        first = await run_workspace(ctx)
        second = await run_workspace(ctx)
        # A code turn's first resolution runs `git worktree add`; a turn makes many
        # file-tool calls, and it must not run once per call.
        assert first is second

    async def test_the_run_claims_the_container_it_is_about_to_work_in(self, tmp_path):
        # Without the claim the only "in use" signal is the exec lock, held for one call
        # out of the dozens a turn makes — and the live-session cap would be free to seal
        # the workspace away in a gap between two of them, taking `node_modules`, `.venv`
        # and `.git` with it (the seal drops those by design).
        caps = _caps(tmp_path)
        ctx = _ctx(caps, conversation_id="conv-a")
        await run_workspace(ctx)
        sessions = caps.get_optional(SandboxSessionManager)
        assert sessions is not None
        assert sessions.holders["conv-a"] is ctx.deps.run


# --- worktree: the worktree, and the one-workspace invariant ------------------------


class TestWorktree:
    async def _ctx(self, tmp_path, *, conversation_id="conv-a", run_id="run-1"):
        root = await _repo(tmp_path / "project")
        caps = _caps(tmp_path, {"proj-1": root})
        return root, _ctx(
            caps,
            conversation_id=conversation_id,
            run_id=run_id,
            project_id="proj-1",
            workspace_kind="worktree",
        )

    async def test_resolves_the_projects_worktree_not_the_operators_tree(self, tmp_path):
        root, ctx = await self._ctx(tmp_path)
        workspace = await run_workspace(ctx)
        assert workspace is not None
        assert workspace.kind == "worktree"
        assert workspace.branch == "ody/conv-a"
        # The assertion the whole design exists for: never the operator's own checkout.
        assert workspace.root != root
        assert (workspace.root / "hello.txt").read_text() == "original\n"

    async def test_the_model_is_told_a_real_host_path(self, tmp_path):
        _root, ctx = await self._ctx(tmp_path)
        workspace = await run_workspace(ctx)
        assert workspace is not None
        # No `/work` here: the shell runs on the host, and a container path would name
        # a file it cannot open.
        shown = workspace.display("src/main.py")
        assert shown == str(workspace.root / "src/main.py")
        assert not shown.startswith(SANDBOX_MOUNT)

    async def test_staged_files_land_inside_the_worktree_and_out_of_the_diff(self, tmp_path):
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

    async def test_a_worktree_without_a_project_has_no_workspace_not_a_sandbox(self, tmp_path):
        # Falling back to the sandbox would be the worst answer: the agent would edit
        # files in a container while its shell tools are refused, and nothing would say
        # why. a worktree thread always has a project, so if one does not it degrades
        # visibly.
        caps = _caps(tmp_path, {})
        bare = _ctx(caps, conversation_id="c", workspace_kind="worktree")
        assert await run_workspace(bare) is None
        # ...and likewise when the project id names nothing.
        ctx = _ctx(
            caps, conversation_id="c", workspace_kind="worktree", project_id="gone", run_id="r2"
        )
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


# --- the shell refuses to run outside the workspaces it belongs to ------------------


class TestShellIsProjectFolderOnly:
    async def test_a_sandbox_run_is_refused_even_if_the_tool_is_reachable(self, tmp_path):
        # The enabled gate normally hides these from a sandbox run. That is a filter,
        # and a filter is the wrong last line of defence between a host command and a
        # sandbox thread — so the toolset checks too. Calling it directly is exactly the
        # scenario a filter regression would produce.
        ctx = _ctx(_caps(tmp_path), conversation_id="conv-a")
        result = await shell_toolset().call_tool("run_command", {"command": "echo hi"}, ctx, None)
        assert "only available in a conversation filed under a project" in str(result)


async def test_the_declaration_is_offered_to_the_model_with_the_workspace_default(tmp_path):
    caps = _caps(tmp_path, {"proj-1": tmp_path})
    ctx = _ctx(caps, conversation_id="conv-a", project_id="proj-1", workspace_kind="worktree")
    tool = (await shell_toolset().get_tools(ctx))["run_command"]
    schema = tool.tool_def.parameters_json_schema
    reach = schema["properties"]["reach"]
    description = str(reach.get("description", "")).lower()
    assert "reach" not in schema.get("required", [])
    assert reach.get("default") == "workspace"
    # The description explains where each answer runs and never the permission level: one
    # string ships at all four, so a promise about an approval outcome is wrong at three.
    assert "vm" in description and "host" in description
    assert "approv" not in description


@pytest.mark.skipif(not can_confine(), reason="needs root and bubblewrap")
class TestShellRunsInTheVm:
    """Commands reach the VM through the project's copy, driven for real on this machine."""

    @pytest.fixture
    async def copies(self, tmp_path):
        async with local_copies(tmp_path / "vm") as live:
            yield live

    def _shell(self, tmp_path, copies, *, kind="checkout", shells=None, project="proj-1"):
        root = tmp_path / project
        if not root.exists():
            make_repo(root)
        ctx = checkout_ctx(root, copies=copies, kind=kind, project_id=project)
        return root, Shell(shell_toolset(shells=shells), ctx)

    @pytest.mark.parametrize("kind", ["checkout", "worktree"])
    async def test_a_refused_command_is_a_retry_not_a_dead_run(self, tmp_path, copies, kind):
        """What the model can act on — a refused command — comes back as `ModelRetry`, so
        the turn continues and the agent tries something else."""
        root = await _repo(tmp_path / "project")
        caps = _caps(tmp_path, {"proj-1": root})
        caps.add(copies, as_type=ProjectCopies)
        ctx = _ctx(caps, conversation_id="conv-a", project_id="proj-1", workspace_kind=kind)
        ctx.tool_call_approved = True
        shell = Shell(shell_toolset(), ctx)

        with pytest.raises(ModelRetry):
            await shell.call("run_command", command="rm -rf /")

        # And the turn is still usable afterwards — a retry left nothing broken behind it.
        assert "ok" in (await shell.call("run_command", command="echo ok"))["stdout"]

    async def test_a_workspace_command_runs_in_the_vm_and_a_host_one_on_the_host(
        self, tmp_path, copies
    ):
        root, shell = self._shell(tmp_path, copies)
        inside = await shell.call("run_command", command="pwd")
        assert inside["stdout"].strip() == "/work"
        assert inside["reach"] == "workspace" and inside["fenced"] is True
        assert "unfenced_reason" not in inside

        outside = await shell.call("run_command", command="pwd", reach="host")
        assert Path(outside["stdout"].strip()).resolve() == root.resolve()
        assert outside["reach"] == "host" and outside["fenced"] is False

    @pytest.mark.parametrize(
        "command",
        [
            "echo $(pwd)",  # a nested command, unreadable rather than escaping
            "sh -c pwd",  # an argument that could itself be a command line
            "echo 'two words' && pwd",  # ...and the same shape spelled innocently
            "echo ok > /etc/nope; pwd",  # a path the structure contradicts
        ],
    )
    async def test_a_command_the_judge_could_not_read_still_runs_in_the_vm(
        self, tmp_path, copies, command
    ):
        # The judge declines these for being *unreadable*, not for reaching anywhere, and a
        # route keyed off the judgement alone would let a command choose where it runs by
        # being written so nothing could read it. Only a `host` declaration leaves the VM.
        _root, shell = self._shell(tmp_path, copies)
        result = await shell.call("run_command", command=command)
        assert result["fenced"] is True and "/work" in result["stdout"]
        assert not Path("/etc/nope").exists()

    async def test_the_tracked_directory_cannot_be_moved_out_of_the_project(self, tmp_path, copies):
        # What a command ends in is what the shell persists, so it adopts it only while it
        # is still under the project root — otherwise a `cd` out would leave every later
        # relative path judged against a directory the command is no longer in.
        shells: list = []
        root, shell = self._shell(tmp_path, copies, shells=shells)
        await shell.call("run_command", command="mkdir -p sub && cd sub")
        bound = shells[-1]  # shells[0] is the root-independent template
        assert bound.project.cwd == root / "sub"  # ...a `cd` inside the project is kept
        await shell.call("run_command", command="cd /tmp")
        assert bound.project.cwd == root / "sub"  # ...and one out is not

    async def test_two_project_roots_do_not_share_a_shell(self, tmp_path, copies):
        # Rooting, not isolation: each shell is compared to the folder it should be bound
        # to, because two shells both rooted at the template would also "not share" state.
        shells: list = []
        toolset = shell_toolset(shells=shells)
        root_a, root_b = make_repo(tmp_path / "a"), make_repo(tmp_path / "b")
        a = Shell(toolset, checkout_ctx(root_a, copies=copies, project_id="a"))
        b = Shell(toolset, checkout_ctx(root_b, copies=copies, project_id="b"))
        await a.call("run_command", command="mkdir -p sub && cd sub")
        assert (await b.call("run_command", command="pwd"))["stdout"].strip() == "/work"
        assert (await a.call("run_command", command="pwd"))["stdout"].strip() == "/work/sub"
        assert {shell.project.cwd for shell in shells[1:]} == {root_a / "sub", root_b}

    async def test_a_shell_handed_back_by_a_finished_run_is_reaped(self, tmp_path, copies):
        shells: list = []
        _root, shell = self._shell(tmp_path, copies, shells=shells)
        vm_job = (await shell.call("start_command", command="sleep 60"))["command_id"]
        host_job = (await shell.call("start_command", command="sleep 60", reach="host"))[
            "command_id"
        ]
        assert (await shell.call("check_command", command_id=vm_job))["status"] == "running"

        await shells[-1].shutdown(copies)

        for command_id in (vm_job, host_job):
            gone = await shell.call("check_command", command_id=command_id)
            assert gone["ok"] is False and command_id in gone["error"]


# --- the host-side staging adapter ---------------------------------------------------


class TestHostFiles:
    def test_a_write_outside_the_root_is_refused(self, tmp_path):
        # A staged filename is operator content; containment is the sandbox's own check,
        # reused rather than re-derived.
        files = HostFiles(tmp_path)
        with pytest.raises(SandboxError):
            files.write_file("../escaped.txt", b"x")

    def test_round_trips_within_the_root(self, tmp_path):
        files = HostFiles(tmp_path)
        files.write_file("nested/a.txt", b"hi")
        assert files.read_file("nested/a.txt") == b"hi"


# --- the resolver's own contract -----------------------------------------------------


class TestResolveWorkspace:
    async def test_no_handles_at_all_is_no_workspace(self):
        assert (
            await resolve_workspace(
                workspace_kind="sandbox",
                project_id=None,
                conversation_id="c",
                workspace_key="c",
                owner_id=OWNER,
                sessions=None,
                projects=None,
                worktrees=None,
            )
            is None
        )

    def test_display_never_doubles_a_separator(self, tmp_path):
        workspace = RunWorkspace(root=tmp_path, kind="sandbox", files=HostFiles(tmp_path))
        assert workspace.display("/attachments/a") == f"{SANDBOX_MOUNT}/attachments/a"
