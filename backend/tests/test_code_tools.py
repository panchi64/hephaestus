"""The code tools: Python in the conversation's VM, the fail-closed path without one, and the
egress request.

Running a program reaches past every permission level's write scope, so a call to the runner
is deferred before it executes (`services/permissions`) — the helper below approves and
continues, because what these tests are about is what the tool does once it runs."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import cast

import pytest
from pydantic import JsonValue
from pydantic_ai import (
    Agent,
    DeferredToolRequests,
    DeferredToolResults,
    RunContext,
    ToolApproved,
    ToolDenied,
)
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage
from pydantic_ai.workspaces import CommandResult, WorkspaceError, WorkspaceTimeoutError

from agent import stream_agent_run
from core.config import Settings
from core.container import ServiceContainer
from runs import Run, RunStream, ToolCompleted, ToolProgress, ToolStarted
from services.egress import EgressPolicy
from services.sandbox import SandboxResult
from services.vm import ConversationMachines
from tools import RunDeps, build_agent_toolsets
from tools import code as code_module
from tools.code import code_toolset

from ._helpers import bodies_of, first_body
from ._json import dump, obj, s


@dataclass
class _Call:
    command: str
    shell: bool
    env: Mapping[str, str] | None
    timeout: float | None


@dataclass
class _Workspace:
    """Records each command it is asked to run and answers with a preset result or error."""

    result: CommandResult = field(
        default_factory=lambda: CommandResult(exit_code=0, stdout="hello from box", stderr="")
    )
    error: WorkspaceError | None = None
    calls: list[_Call] = field(default_factory=list[_Call])
    #: What each command found written beside it, and what is still there afterwards.
    seen: dict[str, bytes] = field(default_factory=dict[str, bytes])
    files: dict[str, bytes] = field(default_factory=dict[str, bytes])

    async def make_dir(self, path: str) -> None:
        del path

    async def write_bytes(self, path: str, data: bytes) -> None:
        self.files[path] = data

    async def remove(self, path: str) -> None:
        _ = self.files.pop(path, None)

    async def run(
        self,
        command: str,
        *,
        shell: bool = False,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> CommandResult:
        self.calls.append(_Call(command, shell, env, timeout))
        self.seen.update(self.files)
        if self.error is not None:
            raise self.error
        return self.result


class _Machines:
    """Stands in for the machine pool: only `running` is asked of it."""

    def __init__(self, *, running: bool = False) -> None:
        self._running: bool = running

    def running(self, _key: str) -> bool:
        return self._running


class FakeEgressPolicy:
    """Records each grant and answers with the workspace's whole allowlist, the way the
    real policy does — the seeded entry stands in for the installation-wide list."""

    def __init__(self) -> None:
        self.granted: list[tuple[str, list[str]]] = []
        self.allowed: set[str] = {"pypi.org"}

    async def allow(self, key: str, domains: Iterable[str]) -> frozenset[str]:
        self.granted.append((key, list(domains)))
        self.allowed |= set(domains)
        return frozenset(self.allowed)

    async def allowed_for(self, _key: str) -> frozenset[str]:
        return frozenset(self.allowed)


def _execute_description() -> str:
    description = code_module.code_toolset().tools["execute"].description
    assert description is not None
    return description


def _one_tool_agent(tool: str) -> Agent[RunDeps, DeferredToolRequests | str]:
    return Agent(
        TestModel(call_tools=[tool]),
        deps_type=RunDeps,
        toolsets=build_agent_toolsets({"code": code_toolset()}),
        output_type=[str, DeferredToolRequests],
    )


def _one_tool_deps(
    run: Run,
    machines: object | None,
    egress: object | None = None,
    *,
    delegated_approved: bool = False,
) -> RunDeps:
    caps = ServiceContainer()
    if machines is not None:
        caps.add(machines, as_type=ConversationMachines)
    if egress is not None:
        caps.add(egress, as_type=EgressPolicy)
    return RunDeps(
        run=run,
        owner_id="operator",
        caps=caps,
        conversation_id="conv-1",
        delegated_approved=delegated_approved,
    )


async def _run_one_tool(
    tool: str,
    *,
    machines: object | None = None,
    egress: object | None = None,
    delegated_approved: bool = False,
) -> Run:
    """Drive one code tool through an agent, approving the deferred call, and return the Run.

    One `announced` set spans both passes so the call is announced exactly once.
    """
    agent = _one_tool_agent(tool)
    run = Run(id="t", kind="chat", owner_id="operator", stream=RunStream())
    deps = _one_tool_deps(run, machines, egress, delegated_approved=delegated_approved)
    announced: set[str] = set()
    async with agent.iter("go", deps=deps) as agent_run:
        await stream_agent_run(agent_run, run, announced=announced)
        assert agent_run.result is not None
        output = agent_run.result.output
        messages = agent_run.result.all_messages()
    if not isinstance(output, DeferredToolRequests) or not output.approvals:
        return run
    approvals: dict[str, ToolApproved | ToolDenied | bool] = {
        call.tool_call_id: ToolApproved() for call in output.approvals
    }
    async with agent.iter(
        None,
        deps=deps,
        message_history=messages,
        deferred_tool_results=DeferredToolResults(approvals=approvals),
    ) as agent_run:
        await stream_agent_run(agent_run, run, announced=announced)
    return run


def _result(run: Run) -> dict[str, JsonValue]:
    return obj(dump(first_body(run, ToolCompleted))["result"])


def _serve(monkeypatch: pytest.MonkeyPatch, workspace: _Workspace) -> None:
    def home_workspace(_deps: RunDeps) -> _Workspace:
        return workspace

    monkeypatch.setattr(code_module, "home_workspace", home_workspace)


async def _run_code(
    monkeypatch: pytest.MonkeyPatch,
    workspace: _Workspace,
    *,
    machines: _Machines | None = None,
    delegated_approved: bool = False,
) -> Run:
    _serve(monkeypatch, workspace)
    return await _run_one_tool(
        "code_execute", machines=machines or _Machines(), delegated_approved=delegated_approved
    )


async def _canned(
    monkeypatch: pytest.MonkeyPatch,
    *,
    exit_code: int = 0,
    stdout: str = "",
    stderr: str = "",
    delegated_approved: bool = False,
) -> dict[str, JsonValue]:
    workspace = _Workspace(result=CommandResult(exit_code=exit_code, stdout=stdout, stderr=stderr))
    run = await _run_code(monkeypatch, workspace, delegated_approved=delegated_approved)
    return _result(run)


async def _hint(
    monkeypatch: pytest.MonkeyPatch, *, exit_code: int, stdout: str = "", stderr: str = ""
) -> str:
    result = await _canned(monkeypatch, exit_code=exit_code, stdout=stdout, stderr=stderr)
    return s(result["error"])


async def test_execution_is_deferred_before_it_runs(monkeypatch: pytest.MonkeyPatch):
    """The VM does not make running arbitrary code a workspace write, so the call is
    deferred for the operator (or, at Auto, the review)."""
    workspace = _Workspace()
    _serve(monkeypatch, workspace)
    agent = _one_tool_agent("code_execute")
    run = Run(id="t", kind="chat", owner_id="operator", stream=RunStream())
    async with agent.iter("go", deps=_one_tool_deps(run, _Machines())) as agent_run:
        await stream_agent_run(agent_run, run)
        assert agent_run.result is not None
        output = agent_run.result.output

    assert isinstance(output, DeferredToolRequests)
    assert [c.tool_name for c in output.approvals] == ["code_execute"]
    assert workspace.calls == []


async def test_execute_code_runs_python_in_the_workspace_shell(monkeypatch: pytest.MonkeyPatch):
    workspace = _Workspace()
    run = await _run_code(monkeypatch, workspace)

    [call] = workspace.calls
    [(program, source)] = workspace.seen.items()
    assert call.shell is True
    assert f"python3 {program} < /dev/null" in call.command
    assert source == b"a"
    assert workspace.files == {}, "the program is removed once it has run"
    assert call.timeout == 30.0
    assert _result(run)["stdout"] == "hello from box"
    assert len(bodies_of(run, ToolStarted)) == 1


async def test_stdin_is_written_beside_the_program(monkeypatch: pytest.MonkeyPatch):
    workspace = _Workspace()
    _serve(monkeypatch, workspace)
    toolset = code_toolset()
    run = Run(id="t", kind="chat", owner_id="operator", stream=RunStream())
    ctx = RunContext(deps=_one_tool_deps(run, _Machines()), model=TestModel(), usage=RunUsage())
    tools = await toolset.get_tools(ctx)

    _ = cast(
        object,
        await toolset.call_tool(
            "execute",
            {"code": "print(1)", "stdin": "in", "timeout_s": 5.0},
            ctx,
            tools["execute"],
        ),
    )

    [call] = workspace.calls
    program = next(path for path in workspace.seen if path.endswith(".py"))
    stdin = next(path for path in workspace.seen if path.endswith(".stdin"))
    assert workspace.seen[program] == b"print(1)"
    assert workspace.seen[stdin] == b"in"
    assert f"python3 {program} < {stdin}" in call.command
    assert workspace.files == {}
    assert call.timeout == 5.0


async def test_a_program_too_large_for_the_environment_still_runs(
    monkeypatch: pytest.MonkeyPatch,
):
    workspace = _Workspace()
    _serve(monkeypatch, workspace)
    toolset = code_toolset()
    run = Run(id="t", kind="chat", owner_id="operator", stream=RunStream())
    ctx = RunContext(deps=_one_tool_deps(run, _Machines()), model=TestModel(), usage=RunUsage())
    tools = await toolset.get_tools(ctx)
    data = "x," * 200_000

    _ = cast(
        object,
        await toolset.call_tool(
            "execute", {"code": "print(1)", "stdin": data}, ctx, tools["execute"]
        ),
    )

    [call] = workspace.calls
    assert call.env is None
    assert data.encode() in workspace.seen.values()


async def test_a_cold_machine_announces_the_vm_boot(monkeypatch: pytest.MonkeyPatch):
    run = await _run_code(monkeypatch, _Workspace(), machines=_Machines(running=False))

    progress = bodies_of(run, ToolProgress)
    assert len(progress) == 1
    assert progress[0].partial is not None
    assert "vm" in progress[0].partial.lower()


async def test_a_running_machine_announces_nothing(monkeypatch: pytest.MonkeyPatch):
    run = await _run_code(monkeypatch, _Workspace(), machines=_Machines(running=True))

    assert bodies_of(run, ToolProgress) == []


async def test_execute_code_fails_closed_without_a_vm():
    run = await _run_one_tool("code_execute", machines=None)
    result = _result(run)
    assert result["ok"] is False
    assert "unavailable" in s(result["error"]).lower()


async def test_a_failed_run_feeds_stderr_and_a_hint_back(monkeypatch: pytest.MonkeyPatch):
    result = await _canned(monkeypatch, exit_code=1, stderr="Traceback: NameError: x")
    assert result["ok"] is False
    assert "NameError" in s(result["stderr"])
    assert "non-zero" in s(result["error"])


async def test_a_timeout_gets_a_legible_hint(monkeypatch: pytest.MonkeyPatch):
    workspace = _Workspace(error=WorkspaceTimeoutError("too slow", stdout="part", stderr=""))
    run = await _run_code(monkeypatch, workspace)

    result = _result(run)
    assert result["timed_out"] is True
    assert result["stdout"] == "part"
    assert "time limit" in s(result["error"])


async def test_a_workspace_failure_feeds_back_instead_of_crashing_the_run(
    monkeypatch: pytest.MonkeyPatch,
):
    workspace = _Workspace(error=WorkspaceError("the VM did not start"))
    run = await _run_code(monkeypatch, workspace)

    result = _result(run)
    assert result["ok"] is False
    assert "the VM did not start" in s(result["error"])


async def test_oom_hint_names_the_configured_memory(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(code_module, "get_settings", lambda: Settings(sandbox_memory_mb=777))
    hint = await _hint(monkeypatch, exit_code=137)
    assert "777 MB" in hint


async def test_sigsegv_hint_is_distinguished_from_oom(monkeypatch: pytest.MonkeyPatch):
    hint = await _hint(monkeypatch, exit_code=139)
    assert "139" in hint and "SIGSEGV" in hint
    assert "memory" not in hint.lower()


async def test_missing_module_hint_states_the_exact_install_mechanics(
    monkeypatch: pytest.MonkeyPatch,
):
    # Guards models botching the install (invented pip flags, folding it into the failing call).
    stderr = (
        "Traceback (most recent call last):\n"
        '  File "<string>", line 1, in <module>\n'
        "ModuleNotFoundError: No module named 'matplotlib'\n"
    )
    hint = await _hint(monkeypatch, exit_code=1, stderr=stderr)
    assert "pip install matplotlib" in hint
    assert "shell" in hint
    assert "--" not in hint.replace(" — ", " ")


@pytest.mark.parametrize(
    "stderr",
    [
        # The name never resolves because the VM resolves only the allowlist.
        "curl: (6) Could not resolve host: evil.example.com",
        "WARNING: Retrying... Temporary failure in name resolution\n",
        # A refused HTTPS CONNECT carries no body.
        "urllib.error.URLError: <urlopen error Tunnel connection failed: 403 Forbidden>\n",
    ],
)
async def test_a_host_the_vm_could_not_reach_points_at_the_egress_request(
    monkeypatch: pytest.MonkeyPatch, stderr: str
):
    hint = await _hint(monkeypatch, exit_code=1, stderr=stderr)
    assert "code_request_egress" in hint
    assert "allowlist" in hint


async def test_a_misspelled_package_is_not_diagnosed_as_a_blocked_host(
    monkeypatch: pytest.MonkeyPatch,
):
    # The index was reached, so reading pip's summary as a fence would waste an approval.
    stderr = (
        "ERROR: Could not find a version that satisfies the requirement pandsa "
        "(from versions: none)\n"
        "ERROR: No matching distribution found for pandsa\n"
    )
    hint = await _hint(monkeypatch, exit_code=1, stderr=stderr)
    assert "code_request_egress" not in hint
    assert "stderr" in hint


async def test_fallback_hint_points_at_the_stream_that_holds_the_error(
    monkeypatch: pytest.MonkeyPatch,
):
    # Some runtimes print errors to stdout with an empty stderr.
    hint = await _hint(monkeypatch, exit_code=128, stdout="something exploded")
    assert "stdout" in hint

    hint = await _hint(monkeypatch, exit_code=3)
    assert "no output" in hint


async def test_execute_description_states_the_live_config_caps(monkeypatch: pytest.MonkeyPatch):
    settings = Settings(sandbox_memory_mb=256, sandbox_cpus=3)
    monkeypatch.setattr(code_module, "get_settings", lambda: settings)

    description = _execute_description()

    assert "256 MB" in description
    assert "3 CPUs" in description
    assert "come back whole" in description


async def test_execute_describes_a_reachable_machine_and_the_one_way_to_widen_it():
    """Only a host off the allowlist has to be asked for; no `network` flag is offered."""
    description = _execute_description()

    assert "code_request_egress" in description
    assert "network=True" not in description
    assert "read-only" not in description.lower()


async def test_execute_points_at_the_file_tools_without_reciting_them():
    """The catalog already describes each `files_*` tool, so this one doesn't."""
    description = _execute_description()

    assert "files_*" in description
    for recited in ("files_read_file", "files_edit_file", "files_list_directory"):
        assert recited not in description
    assert "~/scratch" in description and "pip" in description


async def test_request_egress_records_and_returns_domains():
    policy = FakeEgressPolicy()
    run = await _run_one_tool("code_request_egress", egress=policy)

    assert len(policy.granted) == 1
    key, domains = policy.granted[0]
    assert key == "conv-1"
    assert _result(run)["allowed"] == sorted({"pypi.org", *domains})


async def test_the_tool_that_asks_is_not_offered_inside_a_delegation():
    # Pausing is decided before the body runs and a child run cannot park a call, so the
    # asking tool is withheld rather than refused.
    run = Run(id="t", kind="chat", owner_id="operator", stream=RunStream())
    toolset = code_toolset()

    delegated = _one_tool_deps(run, None, FakeEgressPolicy(), delegated_approved=True)
    ctx = RunContext(deps=delegated, model=TestModel(), usage=RunUsage())
    assert "request_egress" not in await toolset.get_tools(ctx)

    ordinary = _one_tool_deps(run, None, FakeEgressPolicy())
    ctx = RunContext(deps=ordinary, model=TestModel(), usage=RunUsage())
    assert "request_egress" in await toolset.get_tools(ctx)


async def test_a_delegate_is_not_sent_after_tools_it_was_never_offered(
    monkeypatch: pytest.MonkeyPatch,
):
    # A delegate has no namespaced names, so text naming withheld tools would not resolve.
    run = Run(id="t", kind="chat", owner_id="operator", stream=RunStream())
    toolset = code_toolset()

    delegated = _one_tool_deps(run, None, delegated_approved=True)
    ctx = RunContext(deps=delegated, model=TestModel(), usage=RunUsage())
    described = (await toolset.get_tools(ctx))["execute"].tool_def.description or ""
    assert "Your file tools" in described
    assert "code_request_egress" not in described
    assert "files_*" not in described

    result = await _canned(
        monkeypatch,
        exit_code=1,
        stderr="curl: (6) Could not resolve host: evil.example.com",
        delegated_approved=True,
    )
    hint = s(result["error"])
    assert "code_request_egress" not in hint
    assert "report" in hint


async def test_request_egress_degrades_without_a_policy():
    run = await _run_one_tool("code_request_egress")

    result = _result(run)
    assert result["ok"] is False
    assert "allowlist" in s(result["error"])


def test_a_host_the_vm_could_not_resolve_carries_what_the_allow_button_needs():
    result = SandboxResult(
        exit_code=1, stdout="", stderr="curl: (6) Could not resolve host: evil.example.com"
    )

    payload = code_module.exec_result(result, sandboxed=True)

    assert payload["denied_host"] == "evil.example.com"
    assert payload["fenced"] is True
    assert payload["reach"] == "network"
    assert "code_request_egress" in str(payload["fence_note"])


def test_an_ordinary_failure_in_the_vm_carries_no_refusal_fields():
    result = SandboxResult(exit_code=1, stdout="", stderr="boom")

    payload = code_module.exec_result(result, sandboxed=True)

    assert "denied_host" not in payload
    assert "fence_note" not in payload


def test_a_host_run_never_reads_a_vm_refusal():
    result = SandboxResult(
        exit_code=1, stdout="", stderr="curl: (6) Could not resolve host: evil.example.com"
    )

    payload = code_module.exec_result(result, sandboxed=False)

    assert "denied_host" not in payload
