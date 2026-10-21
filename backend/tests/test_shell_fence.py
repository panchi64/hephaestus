"""The `shell` tools' guard and their `host` route.

A command runs in the platform VM, in the agent's home (`tests/test_project_shell.py`
drives that for real), unless it declares `host`, which runs on the operator's machine
itself with no fence. The guard in front of both refuses without a VM and pauses for approval on the
first executing command. The VM is a `FakeVM` here: nothing below reaches it, which is
also what the route assertions check.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from pydantic import JsonValue, TypeAdapter
from pydantic_ai import ModelRetry, RunContext
from pydantic_ai.exceptions import ApprovalRequired

from runs.events import ToolProgress
from services.vm.project import ProjectCopies
from tools.emit import RunEventEmitted
from tools.shell import GATED_TOOLS, shell_toolset

from ._json import s
from .shell_support import Shell, checkout_ctx, make_repo, scratch_sessions
from .vm_fakes import FakeVM

_RESULT = TypeAdapter(dict[str, JsonValue])


async def _call(shell: Shell, name: str, **args: object) -> dict[str, JsonValue]:
    """A tool call that ran: its structured result."""
    return _RESULT.validate_python(await shell.call(name, **args))


async def _refused(shell: Shell, name: str, **args: object) -> str:
    """A tool call the guard turned away before anything was spawned: its message."""
    return TypeAdapter(str).validate_python(await shell.call(name, **args))


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return make_repo(tmp_path / "project")


def _copies(tmp_path: Path, *, available: bool = True) -> tuple[ProjectCopies, FakeVM]:
    vm = FakeVM(tmp_path / "guest", available=available)
    return ProjectCopies(vm, idle_ttl_s=3600, reap_interval_s=3600), vm


async def _shell(repo: Path, tmp_path: Path, *, approved: bool = True) -> tuple[Shell, FakeVM]:
    copies, vm = _copies(tmp_path)
    sessions = await scratch_sessions(tmp_path, vm)
    ctx = checkout_ctx(repo, copies=copies, sessions=sessions, approved=approved)
    return Shell(shell_toolset(), ctx), vm


# --- the guard ----------------------------------------------------------------------------


async def test_without_a_vm_the_tools_refuse_and_nothing_runs(repo: Path, tmp_path: Path):
    marker = tmp_path / "ran"
    shell = Shell(shell_toolset(), checkout_ctx(repo))
    for reach in ("offline", "network", "host"):
        refusal = await _refused(shell, "run_command", command=f"touch {marker}", reach=reach)
        assert "platform VM they run in is not running" in refusal
        assert "it is disabled" in refusal
    assert not marker.exists()


async def test_while_the_vm_is_down_the_tools_refuse_with_its_failure(repo: Path, tmp_path: Path):
    copies, vm = _copies(tmp_path, available=False)
    vm.failure = "no hypervisor"
    shell = Shell(shell_toolset(), checkout_ctx(repo, copies=copies))
    marker = tmp_path / "ran"
    refusal = await _refused(shell, "run_command", command=f"touch {marker}", reach="host")
    assert "no hypervisor" in refusal
    assert not marker.exists()


@pytest.mark.parametrize("name", ["run_command", "start_command"])
async def test_the_first_executing_command_of_a_conversation_asks_the_operator(
    repo: Path, tmp_path: Path, name: str
):
    shell, vm = await _shell(repo, tmp_path, approved=False)
    with pytest.raises(ApprovalRequired):
        _ = await _call(shell, name, command="echo hi")
    assert vm.events == []


async def test_a_command_approved_for_the_conversation_runs_without_asking(
    repo: Path, tmp_path: Path
):
    shell, _vm = await _shell(repo, tmp_path, approved=False)
    shell.ctx.deps.delegated_approved = True
    assert (await _call(shell, "run_command", command="echo hi", reach="host"))["ok"] is True


async def test_checking_on_an_approved_command_does_not_ask_again(repo: Path, tmp_path: Path):
    shell, _vm = await _shell(repo, tmp_path, approved=False)
    checked = await _call(shell, "check_command", command_id="whatever")
    assert checked["ok"] is False and "whatever" in s(checked["error"])


def test_the_executing_tools_are_the_ones_the_approval_scopes_name():
    assert GATED_TOOLS == {"shell_run_command", "shell_start_command"}


# --- a host command ----------------------------------------------------------------------


async def test_a_host_command_runs_on_the_operators_machine_and_not_in_the_vm(
    repo: Path, tmp_path: Path
):
    shell, vm = await _shell(repo, tmp_path)
    result = await _call(shell, "run_command", command="pwd", reach="host")
    assert Path(s(result["stdout"]).strip()).resolve() == repo.resolve()
    assert vm.events == [] and vm.runs == [] and vm.background == {}


async def test_a_host_command_without_the_host_fence_says_why(repo: Path, tmp_path: Path):
    # Tests run with the host fence disabled by setting. An operator reading "unfenced"
    # with nothing beside it would think the gate had broken, so the reason is not optional.
    shell, _vm = await _shell(repo, tmp_path)
    result = await _call(shell, "run_command", command="echo hi", reach="host")
    assert result["ok"] is True and result["stdout"] == "hi\n"
    assert result["reach"] == "host" and result["fenced"] is False
    assert "host fence is not applied" in s(result["unfenced_reason"])
    assert "notes" not in result and "fence_note" not in result


async def test_a_background_host_command_carries_the_same_facts(repo: Path, tmp_path: Path):
    shell, vm = await _shell(repo, tmp_path)
    started = await _call(shell, "start_command", command="sleep 30", reach="host")
    assert started["reach"] == "host" and started["fenced"] is False
    assert "host" in s(started["unfenced_reason"])
    assert vm.events == []
    _ = await _call(shell, "stop_command", command_id=started["command_id"])


async def test_a_host_cd_moves_where_the_next_command_runs(repo: Path, tmp_path: Path):
    shell, _vm = await _shell(repo, tmp_path)
    (repo / "sub").mkdir()
    _ = await _call(shell, "run_command", command="cd sub", reach="host")
    after = await _call(shell, "run_command", command="pwd", reach="host")
    assert s(after["stdout"]).strip().endswith("/sub")


async def test_a_failed_host_cd_leaves_the_directory_where_it_was(repo: Path, tmp_path: Path):
    shell, _vm = await _shell(repo, tmp_path)
    before = await _call(shell, "run_command", command="pwd", reach="host")
    result = await _call(shell, "run_command", command="cd nowhere-at-all", reach="host")
    assert result["ok"] is False and result["exit_code"] != 0
    after = await _call(shell, "run_command", command="pwd", reach="host")
    assert after["stdout"] == before["stdout"]


@pytest.mark.parametrize("reach", ["offline", "host"])
async def test_a_destructive_command_comes_back_as_a_retry(repo: Path, tmp_path: Path, reach: str):
    shell, _vm = await _shell(repo, tmp_path)
    with pytest.raises(ModelRetry):
        _ = await _call(shell, "run_command", command="rm -rf /", reach=reach)
    # ...and the turn is still usable: a refusal spawned nothing to clean up.
    ran = await _call(shell, "run_command", command="echo ok", reach="host")
    assert "ok" in s(ran["stdout"])


async def test_a_host_command_the_os_wont_spawn_is_a_retry_not_a_dead_run(
    repo: Path, tmp_path: Path
):
    shell, _vm = await _shell(repo, tmp_path)
    with pytest.raises(ModelRetry):
        _ = await _call(shell, "run_command", command="echo \x00hi", reach="host")


async def test_a_background_host_command_is_started_checked_and_stopped(repo: Path, tmp_path: Path):
    shell, _vm = await _shell(repo, tmp_path)
    started = await _call(shell, "start_command", command="echo up; sleep 30", reach="host")
    command_id = s(started["command_id"])
    assert started["command"] == "echo up; sleep 30"

    checked = await _call(shell, "check_command", command_id=command_id)
    for _ in range(50):  # the process has to reach its first write
        if "up" in s(checked["stdout"]):
            break
        await asyncio.sleep(0.05)
        checked = await _call(shell, "check_command", command_id=command_id)
    assert "up" in s(checked["stdout"]) and checked["status"] == "running"
    assert checked["exit_code"] is None

    stopped = await _call(shell, "stop_command", command_id=command_id)
    assert stopped["status"] == "stopped"
    # The id is spent: a second stop finds nothing rather than killing a reused pid.
    again = await _call(shell, "stop_command", command_id=command_id)
    assert again["ok"] is False and command_id in s(again["error"])


async def test_a_cancelled_host_command_takes_its_process_tree_with_it(repo: Path, tmp_path: Path):
    shell, _vm = await _shell(repo, tmp_path)
    _ = await _call(shell, "run_command", command="echo warm", reach="host")
    started = tmp_path / "started"
    orphan = tmp_path / "orphan-ran"
    command = f"( sleep 2; touch {orphan} ) & touch {started}; wait"
    task = asyncio.create_task(_call(shell, "run_command", command=command, reach="host"))
    for _ in range(100):
        if started.exists():
            break
        await asyncio.sleep(0.05)
    assert started.exists()  # the tree is up, so cancelling has something to reap

    _ = task.cancel()
    with pytest.raises(asyncio.CancelledError):
        _ = await task
    await asyncio.sleep(2.5)  # past when a survivor would have written
    assert not orphan.exists()


async def test_an_unknown_background_id_is_answered_not_raised(repo: Path, tmp_path: Path):
    shell, _vm = await _shell(repo, tmp_path)
    answered = await _call(shell, "check_command", command_id="nope")
    assert answered["ok"] is False and "nope" in s(answered["error"])


async def test_the_operators_model_keys_are_not_in_a_host_commands_environment(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # Not a boundary — same-user processes can reach the parent's environment through the
    # OS — but the keys must not be sitting in the one place a command reads by accident.
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-appear")
    monkeypatch.setenv("HARMLESS_VAR", "kept")
    shell, _vm = await _shell(repo, tmp_path)
    printed = s(
        (
            await _call(
                shell,
                "run_command",
                command="echo ${OPENAI_API_KEY:-absent} $HARMLESS_VAR",
                reach="host",
            )
        )["stdout"]
    )
    assert "sk-should-not-appear" not in printed
    assert "absent" in printed and "kept" in printed


async def test_a_host_commands_output_reaches_the_operators_stream(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    frames: list[RunEventEmitted] = []

    async def emit(_self: RunContext[object], event: RunEventEmitted) -> RunEventEmitted:
        frames.append(event)
        return event

    monkeypatch.setattr(RunContext, "emit", emit)
    shell, _vm = await _shell(repo, tmp_path)
    shell.ctx.tool_call_id = "call-1"
    result = await _call(
        shell, "run_command", command="echo streaming; sleep 1.2; echo more", reach="host"
    )

    bodies: list[ToolProgress] = []
    for frame in frames:
        assert isinstance(frame.body, ToolProgress)
        bodies.append(frame.body)
    assert bodies and all(body.tool_call_id == "call-1" for body in bodies)
    assert all(body.elapsed_s is not None for body in bodies)
    streamed = "".join(body.partial or "" for body in bodies)
    assert "streaming" in streamed and "more" in streamed
    # No frame repeats what an earlier one already carried.
    assert streamed.count("streaming") == 1
    assert "streaming" in s(result["stdout"])


async def test_a_call_with_nothing_to_attribute_a_frame_to_streams_nothing(
    repo: Path, tmp_path: Path
):
    # A direct call outside a run has no `tool_call_id`, and `RunContext.emit` raises there:
    # no call id means no progress and no failure.
    shell, _vm = await _shell(repo, tmp_path)
    assert shell.ctx.tool_call_id is None
    assert (await _call(shell, "run_command", command="echo quiet", reach="host"))["ok"] is True
