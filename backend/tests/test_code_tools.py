"""The code & shell tools: sandboxed execution, the fail-closed degraded path, and the
host escape hatch (approval-gated on the tool itself, with an explanation).

Running a program reaches past every permission level's write scope, so a call to either
runner is deferred before it executes (`services/permissions`) — the helper below approves
and continues, because what these tests are about is what the tool does once it runs."""

from __future__ import annotations

import tempfile
from collections.abc import Iterable
from pathlib import Path

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

from agent import build_chat_orchestrator, build_resume_orchestrator, stream_agent_run
from core.config import Settings
from core.container import ServiceContainer
from runs import (
    ApprovalRequired,
    Run,
    RunRegistry,
    RunStatus,
    RunStream,
    ToolCompleted,
    ToolProgress,
    ToolStarted,
)
from services.egress import EgressPolicy
from services.sandbox import (
    HostConfinement,
    SandboxError,
    SandboxResult,
    SandboxSessionManager,
    SandboxSpec,
)
from services.vm import DENIED_MARKER
from tools import RunDeps, build_agent_toolsets
from tools import code as code_module
from tools.code import code_toolset

from ._helpers import bodies_of, first_body, parked_turn, run_bodies
from ._json import dump, obj, s


class _CannedSession:
    """A session that returns a preset result or raises a preset error."""

    is_warm: bool = False

    def __init__(
        self,
        *,
        result: SandboxResult | None = None,
        error: Exception | None = None,
        workspace: Path | None = None,
    ) -> None:
        self._result: SandboxResult | None = result
        self._error: Exception | None = error
        self.workspace: Path = workspace or Path(tempfile.gettempdir()) / "unused-canned-workspace"

    def ensure_workspace(self) -> Path:
        return self.workspace

    def read_file(self, relpath: str) -> bytes:
        return (self.workspace / relpath).read_bytes()

    def write_file(self, relpath: str, content: bytes) -> None:
        target = self.workspace / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        _ = target.write_bytes(content)

    async def run(self, _spec: SandboxSpec) -> SandboxResult:
        if self._error is not None:
            raise self._error
        assert self._result is not None
        return self._result


class _CannedManager:
    vm_starting: bool = False

    def __init__(self, session: _CannedSession) -> None:
        self._session: _CannedSession = session
        self.holder: object = None

    async def acquire(self, _key: str, *, holder: object = None) -> _CannedSession:
        self.holder = holder
        return self._session


class FakeSession:
    """Records each spec it runs and returns a canned result."""

    is_warm: bool = False

    def __init__(self) -> None:
        self.specs: list[SandboxSpec] = []

    async def run(self, spec: SandboxSpec) -> SandboxResult:
        self.specs.append(spec)
        return SandboxResult(exit_code=0, stdout="hello from box", stderr="")


class FakeSessionManager:
    """Hands out one session and remembers which key — and which run — it was acquired
    under."""

    def __init__(self) -> None:
        self.session: FakeSession = FakeSession()
        self.acquired: str | None = None
        self.holder: object = None
        self.vm_starting: bool = False

    async def acquire(self, key: str, *, holder: object = None) -> FakeSession:
        self.acquired = key
        self.holder = holder
        return self.session


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
    sessions: object | None,
    egress: object | None = None,
    *,
    delegated_approved: bool = False,
) -> RunDeps:
    caps = ServiceContainer()
    if sessions is not None:
        # The fake manager registers under the class the code tools resolve.
        caps.add(sessions, as_type=SandboxSessionManager)
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
    sessions: object | None = None,
    egress: object | None = None,
    delegated_approved: bool = False,
) -> Run:
    """Drive one code tool through an agent, approving the deferred call, and return the Run.

    One `announced` set spans both passes so the call is announced exactly once.
    """
    agent = _one_tool_agent(tool)
    run = Run(id="t", kind="chat", owner_id="operator", stream=RunStream())
    deps = _one_tool_deps(run, sessions, egress, delegated_approved=delegated_approved)
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


async def test_sandboxed_execution_is_deferred_before_it_runs():
    """The container does not make running arbitrary code a workspace write, so the call
    is deferred for the operator (or, at Auto, the review)."""
    manager = FakeSessionManager()
    agent = _one_tool_agent("code_execute")
    run = Run(id="t", kind="chat", owner_id="operator", stream=RunStream())
    async with agent.iter("go", deps=_one_tool_deps(run, manager)) as agent_run:
        await stream_agent_run(agent_run, run)
        assert agent_run.result is not None
        output = agent_run.result.output

    assert isinstance(output, DeferredToolRequests)
    assert [c.tool_name for c in output.approvals] == ["code_execute"]
    assert manager.session.specs == []


async def test_execute_code_runs_in_the_conversation_session():
    manager = FakeSessionManager()
    run = await _run_one_tool("code_execute", sessions=manager)

    # Holding the session by run keeps the live-session cap from displacing the container.
    assert manager.acquired == "conv-1"
    assert manager.holder is run
    assert len(manager.session.specs) == 1
    spec = manager.session.specs[0]
    assert spec.command[:2] == ["python", "-c"]
    assert _result(run)["stdout"] == "hello from box"
    assert len(bodies_of(run, ToolStarted)) == 1


async def test_cold_session_announces_the_spin_up():
    manager = FakeSessionManager()
    run = await _run_one_tool("code_execute", sessions=manager)

    progress = bodies_of(run, ToolProgress)
    assert len(progress) == 1
    assert progress[0].partial is not None
    assert "sandbox" in progress[0].partial.lower()


async def test_cold_session_announces_the_vm_boot_when_the_vm_is_still_starting():
    manager = FakeSessionManager()
    manager.vm_starting = True
    run = await _run_one_tool("code_execute", sessions=manager)

    progress = bodies_of(run, ToolProgress)
    assert len(progress) == 1
    assert progress[0].partial is not None
    assert "vm" in progress[0].partial.lower()


async def test_execute_code_fails_closed_without_a_runtime():
    run = await _run_one_tool("code_execute", sessions=None)
    result = _result(run)
    assert result["ok"] is False
    assert "unavailable" in s(result["error"]).lower()


def _result(run: Run) -> dict[str, JsonValue]:
    return obj(dump(first_body(run, ToolCompleted))["result"])


async def _run_canned(
    *,
    result: SandboxResult | None = None,
    error: Exception | None = None,
    workspace: Path | None = None,
) -> dict[str, JsonValue]:
    manager = _CannedManager(_CannedSession(result=result, error=error, workspace=workspace))
    run = await _run_one_tool("code_execute", sessions=manager)
    return _result(run)


async def _hint(result: SandboxResult) -> str:
    return s((await _run_canned(result=result))["error"])


async def test_failed_run_feeds_stderr_and_a_hint_back():
    failing = SandboxResult(exit_code=1, stdout="", stderr="Traceback: NameError: x")
    result = await _run_canned(result=failing)
    assert result["ok"] is False
    assert "NameError" in s(result["stderr"])
    assert "non-zero" in s(result["error"])


async def test_timeout_and_oom_get_legible_hints():
    timed = SandboxResult(exit_code=124, stdout="", stderr="", timed_out=True)
    assert "time limit" in await _hint(timed)

    killed = SandboxResult(exit_code=137, stdout="", stderr="")
    assert "memory" in await _hint(killed)


async def test_sandbox_failure_feeds_back_instead_of_crashing_the_run():
    result = await _run_canned(error=SandboxError("container would not start"))
    assert result["ok"] is False
    assert "container would not start" in s(result["error"])


async def test_sigsegv_hint_is_distinguished_from_oom_and_pid_cap():
    crashed = SandboxResult(exit_code=139, stdout="", stderr="")
    hint = await _hint(crashed)
    assert "139" in hint and "SIGSEGV" in hint
    assert "memory" not in hint.lower()
    assert "processes/threads" not in hint


async def test_memory_hint_names_the_actual_configured_cap(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(code_module, "get_settings", lambda: Settings(sandbox_memory="777m"))
    killed = SandboxResult(exit_code=137, stdout="", stderr="")
    hint = await _hint(killed)
    assert "777m" in hint


async def test_pid_cap_hint_is_distinguished_from_plain_nonzero(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(code_module, "get_settings", lambda: Settings(sandbox_pids_limit=64))
    stderr = "OSError: [Errno 11] Resource temporarily unavailable: fork failed"
    failing = SandboxResult(exit_code=1, stdout="", stderr=stderr)
    hint = await _hint(failing)
    assert "processes/threads" in hint
    assert "64" in hint


async def test_missing_module_hint_states_the_exact_install_mechanics():
    # Guards models botching the install (invented pip flags, folding it into the failing call).
    stderr = (
        "Traceback (most recent call last):\n"
        '  File "<string>", line 1, in <module>\n'
        "ModuleNotFoundError: No module named 'matplotlib'\n"
    )
    failing = SandboxResult(exit_code=1, stdout="", stderr=stderr)
    hint = await _hint(failing)
    assert "pip install matplotlib" in hint
    assert "shell" in hint and "reach `network`" in hint
    assert "--" not in hint.replace(" — ", " ")


async def test_a_blocked_host_points_at_the_egress_request():
    # The hint must name the recoverable call, or the model rewrites code that is fine.
    denied = SandboxResult(
        exit_code=1,
        stdout="",
        stderr="urllib.error.HTTPError: HTTP Error 403: hephaestus-egress: denied",
    )
    hint = await _hint(denied)
    assert "code_request_egress" in hint
    assert "allowlist" in hint


async def test_a_resolution_failure_points_at_the_same_egress_request():
    # A client that never reaches the proxy prints a resolution error instead of the marker.
    stderr = (
        "WARNING: Retrying... Temporary failure in name resolution\n"
        "ERROR: No matching distribution found for internal-mirror\n"
    )
    failing = SandboxResult(exit_code=1, stdout="", stderr=stderr)
    hint = await _hint(failing)
    assert "code_request_egress" in hint
    assert "retry this same code unchanged" in hint


async def test_a_refused_tunnel_points_at_the_same_egress_request():
    # A refused HTTPS CONNECT carries no body, so the marker never reaches the client.
    stderr = "urllib.error.URLError: <urlopen error Tunnel connection failed: 403 Forbidden>\n"
    failing = SandboxResult(exit_code=1, stdout="", stderr=stderr)
    hint = await _hint(failing)
    assert "code_request_egress" in hint


async def test_a_misspelled_package_is_not_diagnosed_as_a_blocked_host():
    # The index was reached, so reading pip's summary as a fence would waste an approval.
    stderr = (
        "ERROR: Could not find a version that satisfies the requirement pandsa "
        "(from versions: none)\n"
        "ERROR: No matching distribution found for pandsa\n"
    )
    failing = SandboxResult(exit_code=1, stdout="", stderr=stderr)
    hint = await _hint(failing)
    assert "code_request_egress" not in hint
    assert "stderr" in hint


async def test_fallback_hint_points_at_the_stream_that_holds_the_error():
    # Some runtimes print errors to stdout with an empty stderr.
    stdout_only = SandboxResult(exit_code=128, stdout="something exploded", stderr="")
    hint = await _hint(stdout_only)
    assert "stdout" in hint

    silent = SandboxResult(exit_code=3, stdout="", stderr="")
    hint = await _hint(silent)
    assert "no output" in hint


async def test_execute_description_states_the_live_config_caps(monkeypatch: pytest.MonkeyPatch):
    settings = Settings(
        sandbox_memory="256m",
        sandbox_cpus="0.5",
        sandbox_pids_limit=64,
    )
    monkeypatch.setattr(code_module, "get_settings", lambda: settings)

    description = _execute_description()

    assert "256m" in description
    assert "0.5" in description
    assert "64" in description
    assert "come back whole" in description


async def test_execute_describes_a_reachable_machine_and_the_one_way_to_widen_it():
    """Only a host off the allowlist has to be asked for; no `network` flag is offered."""
    description = _execute_description()

    assert "code_request_egress" in description
    assert "network=True" not in description
    assert "read-only" not in description.lower()
    assert "apt" in description and "not an option" in description


async def test_execute_points_at_the_file_tools_without_reciting_them():
    """The catalog already describes each `files_*` tool, so this one doesn't."""
    description = _execute_description()

    assert "files_*" in description
    for recited in ("files_read_file", "files_edit_file", "files_list_directory"):
        assert recited not in description
    assert "/work" in description and "pip" in description


async def test_request_egress_records_and_returns_domains():
    policy = FakeEgressPolicy()
    run = await _run_one_tool("code_request_egress", egress=policy)

    assert len(policy.granted) == 1
    key, domains = policy.granted[0]
    assert key == "conv-1"
    assert _result(run)["allowed"] == sorted({"pypi.org", *domains})


async def test_the_tools_that_ask_are_not_offered_inside_a_delegation():
    # Pausing is decided before the body runs and a child run cannot park a call, so the
    # asking tools are withheld rather than refused.
    run = Run(id="t", kind="chat", owner_id="operator", stream=RunStream())
    toolset = code_toolset()
    asking = {"request_egress", "run_host_command"}

    delegated = _one_tool_deps(run, None, FakeEgressPolicy(), delegated_approved=True)
    ctx = RunContext(deps=delegated, model=TestModel(), usage=RunUsage())
    assert not asking & set(await toolset.get_tools(ctx))

    ordinary = _one_tool_deps(run, None, FakeEgressPolicy())
    ctx = RunContext(deps=ordinary, model=TestModel(), usage=RunUsage())
    assert asking <= set(await toolset.get_tools(ctx))


async def test_a_delegate_is_not_sent_after_tools_it_was_never_offered():
    # A delegate has no namespaced names, so text naming withheld tools would not resolve.
    run = Run(id="t", kind="chat", owner_id="operator", stream=RunStream())
    toolset = code_toolset()

    delegated = _one_tool_deps(run, None, delegated_approved=True)
    ctx = RunContext(deps=delegated, model=TestModel(), usage=RunUsage())
    described = (await toolset.get_tools(ctx))["execute"].tool_def.description or ""
    assert "Your file tools" in described
    assert "code_request_egress" not in described
    assert "files_*" not in described

    denied = SandboxResult(exit_code=1, stdout="", stderr="hephaestus-egress: denied")
    manager = _CannedManager(_CannedSession(result=denied))
    child = await _run_one_tool("code_execute", sessions=manager, delegated_approved=True)
    hint = s(_result(child)["error"])
    assert "code_request_egress" not in hint
    assert "report" in hint


async def test_request_egress_degrades_without_a_policy():
    run = await _run_one_tool("code_request_egress")

    result = _result(run)
    assert result["ok"] is False
    assert "allowlist" in s(result["error"])


async def test_long_stdout_is_spilled_to_a_pageable_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.setattr(code_module, "get_settings", Settings)
    big = "".join(f"{i:04d}\n" for i in range(10_000))
    result = await _run_canned(
        result=SandboxResult(exit_code=0, stdout=big, stderr="world"), workspace=tmp_path
    )
    assert result["exit_code"] == 0
    assert result["stderr"] == "world"
    stdout = s(result["stdout"])
    assert len(stdout) < len(big)
    assert "/home/agent/scratch/tool-output/" in stdout
    [saved] = (tmp_path / "tool-output").glob("*.txt")
    assert saved.read_text() == big
    assert max(len(line) for line in saved.read_text().splitlines()) == 4


def test_a_refused_host_in_the_vm_carries_what_the_allow_button_needs():
    result = SandboxResult(
        exit_code=1, stdout="", stderr=f"curl: (56) {DENIED_MARKER} evil.example.com"
    )

    payload = code_module.exec_result(result, Settings(), sandboxed=True)

    assert payload["denied_host"] == "evil.example.com"
    assert payload["fenced"] is True
    assert payload["reach"] == "network"
    assert "code_request_egress" in str(payload["fence_note"])


def test_an_ordinary_failure_in_the_vm_carries_no_refusal_fields():
    result = SandboxResult(exit_code=1, stdout="", stderr="boom")

    payload = code_module.exec_result(result, Settings(), sandboxed=True)

    assert "denied_host" not in payload
    assert "fence_note" not in payload


async def test_host_command_output_is_returned_whole():
    reg = RunRegistry()
    orch = build_chat_orchestrator(
        "change the host",
        model=TestModel(call_tools=["code_run_host_command"]),
        categories={"code": code_toolset()},
    )
    run = reg.submit(kind="chat", owner_id="operator", orchestrator=orch)
    await run.wait()
    parked = parked_turn(run)
    call_id = parked.requests.approvals[0].tool_call_id
    big_echo = "x" * 20_000
    decision = {
        call_id: ToolApproved(override_args={"command": f"echo {big_echo}", "explanation": "x"})
    }
    _ = await reg.resume(run.id, build_resume_orchestrator(parked, decision))
    await run.wait()

    assert s(_result(run)["stdout"]).strip() == big_echo


async def test_host_command_parks_with_an_explanation():
    reg = RunRegistry()
    orch = build_chat_orchestrator(
        "change the host",
        model=TestModel(call_tools=["code_run_host_command"]),
        categories={"code": code_toolset()},
    )
    run = reg.submit(kind="chat", owner_id="operator", orchestrator=orch)
    await run.wait()

    assert run.status is RunStatus.awaiting_input
    approval = first_body(run, ApprovalRequired)
    assert "run_host_command" in approval.name
    assert approval.explanation is not None
    assert "tool.completed" not in [b.type for b in run_bodies(run)]


async def test_approved_host_command_runs_on_host():
    reg = RunRegistry()
    orch = build_chat_orchestrator(
        "change the host",
        model=TestModel(call_tools=["code_run_host_command"]),
        categories={"code": code_toolset()},
    )
    run = reg.submit(kind="chat", owner_id="operator", orchestrator=orch)
    await run.wait()
    parked = parked_turn(run)
    call_id = parked.requests.approvals[0].tool_call_id

    safe = {"command": "echo HOSTRAN", "explanation": "x"}
    decision = {call_id: ToolApproved(override_args=safe)}
    _ = await reg.resume(run.id, build_resume_orchestrator(parked, decision))
    await run.wait()

    assert run.status is RunStatus.done
    assert "HOSTRAN" in s(_result(run)["stdout"])


async def test_the_hatch_is_fenced_with_the_installations_allowlist_not_the_threads(
    monkeypatch: pytest.MonkeyPatch,
):
    """One proxy fences every confined host process, so a thread's grant must not widen it."""
    egress = FakeEgressPolicy()
    _ = await egress.allow("conv-1", ["files.example.com"])
    seen: list[HostConfinement | None] = []

    async def capture(
        _command: str, *, confinement: HostConfinement | None = None, **_kwargs: object
    ) -> SandboxResult:
        seen.append(confinement)
        return SandboxResult(exit_code=0, stdout="", stderr="")

    async def active(_settings: Settings) -> HostConfinement:
        return HostConfinement(True, allow_write=("/tmp",), allowed_domains=("pypi.org",))

    monkeypatch.setattr("tools.code.run_on_host", capture)
    monkeypatch.setattr("tools.code.resolve_confinement", active)

    run = Run(id="run-1", kind="chat", owner_id="operator", stream=RunStream())
    ctx = RunContext(deps=_one_tool_deps(run, None, egress), model=TestModel(), usage=RunUsage())
    toolset = code_toolset()
    tools = await toolset.get_tools(ctx)
    await toolset.call_tool(
        "run_host_command",
        {"command": "echo hi", "explanation": "x"},
        ctx,
        tools["run_host_command"],
    )

    [confinement] = seen
    assert confinement is not None
    assert set(confinement.allowed_domains) == {"pypi.org"}
    assert confinement.allow_write == ("/tmp",)


async def test_an_approved_host_command_runs_in_its_own_scratch_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """Not the backend's source tree (`cat .env` would read its keys), and not the run's
    workspace, which sits under `data_dir` where the host fence denies access."""
    monkeypatch.setattr(code_module, "host_scratch_dir", lambda: tmp_path)
    _ = (tmp_path / "here.txt").write_text("IN THE SCRATCH DIRECTORY")

    reg = RunRegistry()
    orch = build_chat_orchestrator(
        "read the file",
        model=TestModel(call_tools=["code_run_host_command"]),
        categories={"code": code_toolset()},
    )
    run = reg.submit(kind="chat", owner_id="operator", orchestrator=orch)
    await run.wait()
    parked = parked_turn(run)
    call_id = parked.requests.approvals[0].tool_call_id
    decision = {
        call_id: ToolApproved(override_args={"command": "cat here.txt", "explanation": "x"})
    }
    _ = await reg.resume(run.id, build_resume_orchestrator(parked, decision))
    await run.wait()

    assert "IN THE SCRATCH DIRECTORY" in s(_result(run)["stdout"])
