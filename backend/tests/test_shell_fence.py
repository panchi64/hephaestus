"""The `shell` tools' guard and their `host` route.

A command in a project chat runs in the platform VM (`tests/test_project_shell.py` drives
that for real) unless it declares `host`, which runs on the operator's machine itself with
no fence. The guard in front of both refuses without a VM and pauses for approval on the
first executing command. The VM is a `FakeVM` here: nothing below reaches it, which is
also what the route assertions check.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from pydantic_ai import ModelRetry, RunContext
from pydantic_ai.exceptions import ApprovalRequired

from runs.events import ToolProgress
from services.vm.project import ProjectCopies
from tools.emit import RunEventEmitted
from tools.shell import GATED_TOOLS, shell_toolset

from .shell_support import Shell, checkout_ctx, make_repo
from .vm_fakes import FakeVM


@pytest.fixture
def repo(tmp_path) -> Path:
    return make_repo(tmp_path / "project")


def _copies(tmp_path: Path, *, available: bool = True) -> tuple[ProjectCopies, FakeVM]:
    vm = FakeVM(tmp_path / "guest", available=available)
    return ProjectCopies(vm, idle_ttl_s=3600, reap_interval_s=3600), vm


def _shell(repo: Path, tmp_path: Path, *, approved: bool = True) -> tuple[Shell, FakeVM]:
    copies, vm = _copies(tmp_path)
    return Shell(shell_toolset(), checkout_ctx(repo, copies=copies, approved=approved)), vm


# --- the guard ----------------------------------------------------------------------------


async def test_without_a_vm_the_tools_refuse_and_nothing_runs(repo, tmp_path):
    marker = tmp_path / "ran"
    shell = Shell(shell_toolset(), checkout_ctx(repo))
    for reach in ("workspace", "network", "host"):
        refusal = await shell.call("run_command", command=f"touch {marker}", reach=reach)
        assert "platform VM they run in is not running" in refusal
        assert "it is disabled" in refusal
    assert not marker.exists()


async def test_while_the_vm_is_down_the_tools_refuse_with_its_failure(repo, tmp_path):
    copies, vm = _copies(tmp_path, available=False)
    vm.failure = "no hypervisor"
    shell = Shell(shell_toolset(), checkout_ctx(repo, copies=copies))
    marker = tmp_path / "ran"
    refusal = await shell.call("run_command", command=f"touch {marker}", reach="host")
    assert "no hypervisor" in refusal
    assert not marker.exists()


@pytest.mark.parametrize("name", ["run_command", "start_command"])
async def test_the_first_executing_command_of_a_conversation_asks_the_operator(
    repo, tmp_path, name
):
    shell, vm = _shell(repo, tmp_path, approved=False)
    with pytest.raises(ApprovalRequired):
        await shell.call(name, command="echo hi")
    assert vm.events == []


async def test_a_command_approved_for_the_conversation_runs_without_asking(repo, tmp_path):
    shell, _vm = _shell(repo, tmp_path, approved=False)
    shell.ctx.deps.delegated_approved = True
    assert (await shell.call("run_command", command="echo hi", reach="host"))["ok"] is True


async def test_checking_on_an_approved_command_does_not_ask_again(repo, tmp_path):
    shell, _vm = _shell(repo, tmp_path, approved=False)
    checked = await shell.call("check_command", command_id="whatever")
    assert checked["ok"] is False and "whatever" in checked["error"]


def test_the_executing_tools_are_the_ones_the_approval_scopes_name():
    assert GATED_TOOLS == {"shell_run_command", "shell_start_command"}


# --- a host command ----------------------------------------------------------------------


async def test_a_host_command_runs_on_the_operators_machine_and_not_in_the_vm(repo, tmp_path):
    shell, vm = _shell(repo, tmp_path)
    result = await shell.call("run_command", command="pwd", reach="host")
    assert Path(result["stdout"].strip()).resolve() == repo.resolve()
    assert vm.events == [] and vm.runs == [] and vm.background == {}


async def test_a_host_declaration_is_unfenced_and_says_it_asked_to_be(repo, tmp_path):
    # An operator reading "unfenced" with nothing beside it would be right to read it as
    # the gate having broken, so the reason is not optional.
    shell, _vm = _shell(repo, tmp_path)
    result = await shell.call("run_command", command="echo hi", reach="host")
    assert result["ok"] is True and result["stdout"] == "hi\n"
    assert result["reach"] == "host" and result["fenced"] is False
    assert "host" in result["unfenced_reason"]
    assert "notes" not in result and "fence_note" not in result


async def test_a_background_host_command_carries_the_same_facts(repo, tmp_path):
    shell, vm = _shell(repo, tmp_path)
    started = await shell.call("start_command", command="sleep 30", reach="host")
    assert started["reach"] == "host" and started["fenced"] is False
    assert "host" in started["unfenced_reason"]
    assert vm.events == []
    await shell.call("stop_command", command_id=started["command_id"])


async def test_a_host_cd_moves_where_the_next_command_runs(repo, tmp_path):
    shell, _vm = _shell(repo, tmp_path)
    (repo / "sub").mkdir()
    await shell.call("run_command", command="cd sub", reach="host")
    after = await shell.call("run_command", command="pwd", reach="host")
    assert after["stdout"].strip().endswith("/sub")


async def test_a_failed_host_cd_leaves_the_directory_where_it_was(repo, tmp_path):
    shell, _vm = _shell(repo, tmp_path)
    before = await shell.call("run_command", command="pwd", reach="host")
    result = await shell.call("run_command", command="cd nowhere-at-all", reach="host")
    assert result["ok"] is False and result["exit_code"] != 0
    after = await shell.call("run_command", command="pwd", reach="host")
    assert after["stdout"] == before["stdout"]


@pytest.mark.parametrize("reach", ["workspace", "host"])
async def test_a_destructive_command_comes_back_as_a_retry(repo, tmp_path, reach):
    shell, _vm = _shell(repo, tmp_path)
    with pytest.raises(ModelRetry):
        await shell.call("run_command", command="rm -rf /", reach=reach)
    # ...and the turn is still usable: a refusal spawned nothing to clean up.
    assert "ok" in (await shell.call("run_command", command="echo ok", reach="host"))["stdout"]


async def test_a_host_command_the_os_wont_spawn_is_a_retry_not_a_dead_run(repo, tmp_path):
    shell, _vm = _shell(repo, tmp_path)
    with pytest.raises(ModelRetry):
        await shell.call("run_command", command="echo \x00hi", reach="host")


async def test_a_background_host_command_is_started_checked_and_stopped(repo, tmp_path):
    shell, _vm = _shell(repo, tmp_path)
    started = await shell.call("start_command", command="echo up; sleep 30", reach="host")
    command_id = started["command_id"]
    assert started["command"] == "echo up; sleep 30"

    checked = await shell.call("check_command", command_id=command_id)
    for _ in range(50):  # the process has to reach its first write
        if "up" in checked["stdout"]:
            break
        await asyncio.sleep(0.05)
        checked = await shell.call("check_command", command_id=command_id)
    assert "up" in checked["stdout"] and checked["status"] == "running"
    assert checked["exit_code"] is None

    stopped = await shell.call("stop_command", command_id=command_id)
    assert stopped["status"] == "stopped"
    # The id is spent: a second stop finds nothing rather than killing a reused pid.
    again = await shell.call("stop_command", command_id=command_id)
    assert again["ok"] is False and command_id in again["error"]


async def test_a_cancelled_host_command_takes_its_process_tree_with_it(repo, tmp_path):
    shell, _vm = _shell(repo, tmp_path)
    await shell.call("run_command", command="echo warm", reach="host")
    started = tmp_path / "started"
    orphan = tmp_path / "orphan-ran"
    command = f"( sleep 2; touch {orphan} ) & touch {started}; wait"
    task = asyncio.create_task(shell.call("run_command", command=command, reach="host"))
    for _ in range(100):
        if started.exists():
            break
        await asyncio.sleep(0.05)
    assert started.exists()  # the tree is up, so cancelling has something to reap

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(2.5)  # past when a survivor would have written
    assert not orphan.exists()


async def test_an_unknown_background_id_is_answered_not_raised(repo, tmp_path):
    shell, _vm = _shell(repo, tmp_path)
    answered = await shell.call("check_command", command_id="nope")
    assert answered["ok"] is False and "nope" in answered["error"]


async def test_the_operators_model_keys_are_not_in_a_host_commands_environment(
    repo, tmp_path, monkeypatch
):
    # Not a boundary — same-user processes can reach the parent's environment through the
    # OS — but the keys must not be sitting in the one place a command reads by accident.
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-appear")
    monkeypatch.setenv("HARMLESS_VAR", "kept")
    shell, _vm = _shell(repo, tmp_path)
    printed = (
        await shell.call(
            "run_command", command="echo ${OPENAI_API_KEY:-absent} $HARMLESS_VAR", reach="host"
        )
    )["stdout"]
    assert "sk-should-not-appear" not in printed
    assert "absent" in printed and "kept" in printed


async def test_a_host_commands_output_reaches_the_operators_stream(repo, tmp_path, monkeypatch):
    frames: list[RunEventEmitted] = []

    async def emit(_self, event: RunEventEmitted):
        frames.append(event)
        return event

    monkeypatch.setattr(RunContext, "emit", emit)
    shell, _vm = _shell(repo, tmp_path)
    shell.ctx.tool_call_id = "call-1"
    result = await shell.call(
        "run_command", command="echo streaming; sleep 1.2; echo more", reach="host"
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
    assert "streaming" in result["stdout"]


async def test_a_call_with_nothing_to_attribute_a_frame_to_streams_nothing(repo, tmp_path):
    # A direct call outside a run has no `tool_call_id`, and `RunContext.emit` raises there:
    # no call id means no progress and no failure.
    shell, _vm = _shell(repo, tmp_path)
    assert shell.ctx.tool_call_id is None
    assert (await shell.call("run_command", command="echo quiet", reach="host"))["ok"] is True
