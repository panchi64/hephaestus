"""What a restarted VM and a refused tunnel tell the model, on every path a result takes."""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest
from pydantic import JsonValue, TypeAdapter

from services.home.paths import SCRATCH
from services.sandbox import SandboxSpec
from services.sandbox.base import safe_key
from services.sandbox.home_shell import HomeShell
from services.vm.driver import ExecResult
from services.vm.platform import JobState
from services.vm.project import ProjectCopies
from services.vm.workspace import LOST_TO_RESTART
from tools.shell import shell_toolset

from ._json import arr, s
from .shell_support import Shell, checkout_ctx, scratch_sessions
from .vm_fakes import FakeVM

_RESULT = TypeAdapter(dict[str, JsonValue])


async def test_a_background_commands_final_status_carries_the_restart_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    vm = FakeVM(tmp_path / "guest")
    vm.alive_after_start = False
    copies = ProjectCopies(vm, idle_ttl_s=3600, reap_interval_s=3600)
    sessions = await scratch_sessions(tmp_path, vm)
    session = await sessions.acquire("conv-1")
    shell = HomeShell(default_timeout=30.0, max_output_chars=10_000, start=SCRATCH.path)
    command_id = await shell.start(session, copies, "make", mounts=[])
    real_job = vm.job

    async def rebuilt_after_it_finished(
        name: str, *, out_offset: int = 0, err_offset: int = 0, tail: bool = False
    ) -> JobState:
        state = await real_job(name, out_offset=out_offset, err_offset=err_offset, tail=tail)
        vm.restart()
        return state

    monkeypatch.setattr(vm, "job", rebuilt_after_it_finished)

    status = await shell.check(copies, command_id)

    assert status is not None and status.status == "finished"
    assert status.notes == (LOST_TO_RESTART,)


async def test_code_execute_results_carry_the_restart_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    vm = FakeVM(tmp_path / "guest")
    sessions = await scratch_sessions(tmp_path, vm)
    session = await sessions.acquire("conv-1")
    real_run = vm.run

    async def rebuilt_after_the_run(
        key: str,
        argv: Sequence[str],
        *,
        env: Mapping[str, str],
        timeout_s: float,
        stdin: str | None = None,
    ) -> ExecResult:
        result = await real_run(key, argv, env=env, timeout_s=timeout_s, stdin=stdin)
        vm.restart()
        return result

    monkeypatch.setattr(vm, "run", rebuilt_after_the_run)

    result = await session.run(SandboxSpec(command=["echo", "hi"], timeout_s=10))

    assert result.notes == (LOST_TO_RESTART,)


async def test_a_refusal_the_client_did_not_name_is_taken_from_the_gateways_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    vm = FakeVM(tmp_path / "guest")
    vm.alive_after_start = False
    project = tmp_path / "project"
    project.mkdir()
    copies = ProjectCopies(vm, idle_ttl_s=3600, reap_interval_s=3600)
    sessions = await scratch_sessions(tmp_path, vm)
    ctx = checkout_ctx(project, copies=copies, sessions=sessions, conversation_id="conv-a")
    credentials = vm.gateway.grant(tmp_path / safe_key("conv-a"))
    grant = vm.gateway.grant_for(credentials)
    assert grant is not None
    grant.denials.append((time.monotonic() - 100, "stale.example"))
    real_read = vm.read_guest

    async def refused_meanwhile(guest: str, *, max_bytes: int) -> str:
        grant.denials.append((time.monotonic(), "files.example.org"))
        return await real_read(guest, max_bytes=max_bytes)

    monkeypatch.setattr(vm, "read_guest", refused_meanwhile)

    result = _RESULT.validate_python(
        await Shell(shell_toolset(), ctx).call("run_command", command="pip install x")
    )

    assert result["ok"] is False
    assert result["denied_host"] == "files.example.org"
    assert "files.example.org" in s(result["fence_note"])
    assert LOST_TO_RESTART not in [s(n) for n in arr(result.get("notes", []))]
