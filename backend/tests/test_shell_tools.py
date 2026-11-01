"""The `shell` category: which calls ask first, where the shell stands afterwards, and the
host tool's fence and starting folder."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import JsonValue, TypeAdapter
from pydantic_ai import RunContext
from pydantic_ai.exceptions import ApprovalRequired
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage
from pydantic_ai.workspaces import LocalWorkspaceBackend, Workspace

from core.config import Settings
from core.container import ServiceContainer
from runs import Run, RunStream
from services.home.backend import Mount
from services.home.paths import SCRATCH, Place
from services.home.table import RunHome
from services.sandbox import HostConfinement, HostExecutionError, SandboxResult
from tools import RunDeps
from tools import shell as shell_module
from tools.shell import ShellCategory

_PAYLOAD = TypeAdapter(dict[str, JsonValue])


def _deps(*, delegated: bool = False) -> RunDeps:
    run = Run(id="run-1", kind="chat", owner_id="operator", stream=RunStream())
    return RunDeps(
        run=run,
        owner_id="operator",
        caps=ServiceContainer(),
        conversation_id="conv-1",
        delegated_approved=delegated,
    )


def _ctx(deps: RunDeps, root: Path, *, approved: bool = False) -> RunContext[RunDeps]:
    ctx = RunContext(
        deps=deps,
        workspace=Workspace(LocalWorkspaceBackend(root)),
        model=TestModel(),
        usage=RunUsage(),
        run_id="r1",
    )
    ctx.tool_call_approved = approved
    return ctx


async def _call(
    toolset: ShellCategory, ctx: RunContext[RunDeps], name: str, args: dict[str, object]
) -> object:
    tools = await toolset.get_tools(ctx)
    return await toolset.call_tool(name, args, ctx, tools[name])


class TestWhichCallsAsk:
    @pytest.mark.parametrize(
        ("name", "args"),
        [("run_command", {"command": "true"}), ("start_command", {"command": "true"})],
    )
    async def test_a_call_that_executes_something_new_asks_first(
        self, tmp_path: Path, name: str, args: dict[str, object]
    ):
        ctx = _ctx(_deps(), tmp_path)
        with pytest.raises(ApprovalRequired):
            _ = await _call(ShellCategory(), ctx, name, args)

    @pytest.mark.parametrize("name", ["check_command", "stop_command"])
    async def test_a_call_on_a_process_already_approved_does_not_ask(
        self, tmp_path: Path, name: str
    ):
        ctx = _ctx(_deps(), tmp_path)
        try:
            _ = await _call(ShellCategory(), ctx, name, {"command_id": "none"})
        except ApprovalRequired:
            pytest.fail(f"{name} asked for approval")

    async def test_an_approved_call_runs(self, tmp_path: Path):
        ctx = _ctx(_deps(), tmp_path, approved=True)
        out = await _call(ShellCategory(), ctx, "run_command", {"command": "echo hello"})
        assert "hello" in str(out)

    async def test_a_delegated_approved_run_does_not_ask(self, tmp_path: Path):
        ctx = _ctx(_deps(delegated=True), tmp_path)
        out = await _call(ShellCategory(), ctx, "run_command", {"command": "echo delegated"})
        assert "delegated" in str(out)

    async def test_the_shell_tools_that_execute_are_the_ones_a_conversation_may_grant(self):
        assert shell_module.GATED_TOOLS == {"shell_run_command", "shell_start_command"}


class TestTheHostTool:
    async def test_it_always_asks(self, tmp_path: Path):
        tools = await ShellCategory().get_tools(_ctx(_deps(), tmp_path))
        assert tools["run_on_host"].tool_def.kind == "unapproved"

    async def test_it_is_withheld_from_a_delegated_run(self, tmp_path: Path):
        toolset = ShellCategory()
        ordinary = await toolset.get_tools(_ctx(_deps(), tmp_path))
        delegated = await toolset.get_tools(_ctx(_deps(delegated=True), tmp_path))
        assert "run_on_host" in ordinary
        assert "run_on_host" not in delegated
        assert "run_command" in delegated


class TestTheShellsWorkingDirectory:
    async def test_it_is_recorded_after_a_call(self, tmp_path: Path):
        (tmp_path / "sub").mkdir()
        deps = _deps()
        ctx = _ctx(deps, tmp_path, approved=True)
        toolset = ShellCategory()
        assert deps.shell_cwd is None

        _ = await _call(toolset, ctx, "run_command", {"command": "cd sub"})

        assert deps.shell_cwd is not None
        assert Path(deps.shell_cwd).resolve() == (tmp_path / "sub").resolve()

    async def test_it_is_recorded_even_when_the_call_raises(self, tmp_path: Path):
        deps = _deps()
        ctx = _ctx(deps, tmp_path)
        with pytest.raises(ApprovalRequired):
            _ = await _call(ShellCategory(), ctx, "run_command", {"command": "true"})
        assert deps.shell_cwd is None


def _home(root: Path, place: Place, default: Place) -> RunHome:
    mounts = (Mount(SCRATCH, root / "scratch", writable=True), Mount(place, root, writable=True))
    return RunHome(mounts=mounts, default=default, key="t")


class _HostRun:
    """Stands in for `run_on_host` and `resolve_confinement`, recording the starting folder."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, home: RunHome | None) -> None:
        self.cwd: Path | None = None
        self.confinement: HostConfinement | None = None
        self.result: SandboxResult = SandboxResult(exit_code=0, stdout="hi", stderr="")
        self._confinement: HostConfinement = HostConfinement(True)

        async def resolved(_settings: Settings) -> HostConfinement:
            return self._confinement

        async def ran(
            _command: str,
            *,
            confinement: HostConfinement | None = None,
            cwd: Path | None = None,
            **_kwargs: object,
        ) -> SandboxResult:
            self.confinement = confinement
            self.cwd = cwd
            return self.result

        async def resolve_home(_deps: RunDeps) -> RunHome | None:
            return home

        monkeypatch.setattr(shell_module, "run_on_host", ran)
        monkeypatch.setattr(shell_module, "resolve_confinement", resolved)
        monkeypatch.setattr(shell_module, "resolve_run_home", resolve_home)
        monkeypatch.setattr(shell_module, "host_scratch_dir", lambda: Path("/host-scratch"))

    def fence_with(self, confinement: HostConfinement) -> None:
        self._confinement = confinement

    async def call(self, tmp_path: Path) -> dict[str, JsonValue]:
        ctx = _ctx(_deps(), tmp_path)
        return _PAYLOAD.validate_python(await shell_module.run_on_host_tool(ctx, "x", "x"))


class TestWhereAHostCommandStarts:
    async def test_in_the_host_folder_of_the_threads_project(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        project = Place("projects", "app")
        host = _HostRun(monkeypatch, _home(tmp_path / "app", project, project))
        _ = await host.call(tmp_path)
        assert host.cwd == tmp_path / "app"

    async def test_in_the_host_folder_of_the_threads_worktree(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        worktree = Place("worktrees", "app")
        host = _HostRun(monkeypatch, _home(tmp_path / "wt", worktree, worktree))
        _ = await host.call(tmp_path)
        assert host.cwd == tmp_path / "wt"

    async def test_in_the_host_scratch_directory_when_the_default_is_scratch(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        host = _HostRun(monkeypatch, _home(tmp_path / "app", Place("projects", "app"), SCRATCH))
        _ = await host.call(tmp_path)
        assert host.cwd == Path("/host-scratch")

    async def test_in_the_host_scratch_directory_without_a_home(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        host = _HostRun(monkeypatch, None)
        _ = await host.call(tmp_path)
        assert host.cwd == Path("/host-scratch")


class TestTheHostPayload:
    async def test_it_is_fenced_with_the_installations_allowlist(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        host = _HostRun(monkeypatch, None)
        fence = HostConfinement(True, allow_write=("/tmp",), allowed_domains=("pypi.org",))
        host.fence_with(fence)

        _ = await host.call(tmp_path)

        assert host.confinement == fence

    async def test_a_fenced_command_carries_the_shells_fields(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        host = _HostRun(monkeypatch, None)
        host.result = SandboxResult(
            exit_code=1, stdout="", stderr="touch: /Users/t/.zshrc: Operation not permitted"
        )

        payload = await host.call(tmp_path)

        assert payload["fenced"] is True
        assert payload["reach"] == "host"
        assert "unfenced_reason" not in payload
        assert "fence_note" in payload

    async def test_an_unfenced_command_says_why(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        host = _HostRun(monkeypatch, None)
        host.fence_with(HostConfinement(False, reason="bubblewrap is missing"))

        payload = await host.call(tmp_path)

        assert payload["fenced"] is False
        assert "bubblewrap is missing" in str(payload["unfenced_reason"])
        assert "fence_note" not in payload

    async def test_a_launch_failure_comes_back_as_an_error(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        _ = _HostRun(monkeypatch, None)

        async def failing(_command: str, **_kwargs: object) -> SandboxResult:
            raise HostExecutionError("no such shell")

        monkeypatch.setattr(shell_module, "run_on_host", failing)
        ctx = _ctx(_deps(), tmp_path)

        payload = _PAYLOAD.validate_python(await shell_module.run_on_host_tool(ctx, "x", "x"))

        assert payload["ok"] is False
        assert "no such shell" in str(payload["error"])
