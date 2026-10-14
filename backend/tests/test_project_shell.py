"""The project shell end to end: the `shell` tools, the project sync and the guest scripts.

Commands run in a platform VM whose guest is this machine (`vm_local`), against a temp git
repository. What is asserted is what the operator and the model can observe: files and
commits landing in the host repository, the working directory carried between commands,
the network a command is (not) given, and background commands.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import cast

import pytest
from pydantic import JsonValue
from pydantic_ai import RunContext

from runs import ToolProgress
from services.egress import EgressPolicy
from services.vm.project import ProjectCopies
from tools.emit import RunEventEmitted
from tools.shell import shell_toolset

from ._json import arr, s
from .conftest import egress_policy
from .shell_support import Shell, checkout_ctx, git, local_copies, make_repo
from .vm_local import can_confine

pytestmark = pytest.mark.skipif(not can_confine(), reason="needs root and bubblewrap")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return make_repo(tmp_path / "project")


@pytest.fixture
async def copies(tmp_path: Path) -> AsyncIterator[ProjectCopies]:
    async with local_copies(tmp_path / "vm") as live:
        yield live


@pytest.fixture
def egress(tmp_path: Path) -> EgressPolicy:
    return egress_policy(tmp_path / "data", ("pypi.org",))


def _shell(repo: Path, copies: ProjectCopies, egress: EgressPolicy | None = None) -> Shell:
    return Shell(shell_toolset(), checkout_ctx(repo, copies=copies, egress=egress))


async def _call(shell: Shell, name: str, **args: str | int) -> dict[str, JsonValue]:
    """A shell tool's result for a command that ran (a refusal comes back as a string)."""
    return cast(dict[str, JsonValue], await shell.call(name, **args))


async def test_a_workspace_command_edits_a_file_and_it_lands_in_the_host_repo(
    repo: Path, copies: ProjectCopies
):
    shell = _shell(repo, copies)
    result = await _call(
        shell, "run_command", command="echo changed > hello.txt && echo new > added.txt"
    )
    assert result["ok"] is True
    assert result["reach"] == "workspace" and result["fenced"] is True
    assert (repo / "hello.txt").read_text() == "changed\n"
    assert (repo / "added.txt").read_text() == "new\n"


async def test_a_command_runs_in_the_vm_copy_not_in_the_host_folder(
    repo: Path, copies: ProjectCopies
):
    shell = _shell(repo, copies)
    result = await _call(shell, "run_command", command="pwd; ls")
    stdout = s(result["stdout"])
    assert stdout.splitlines()[0] == "/work"
    assert str(repo) not in stdout
    assert "hello.txt" in stdout


async def test_a_cd_into_a_subdirectory_persists_to_the_next_command(
    repo: Path, copies: ProjectCopies
):
    shell = _shell(repo, copies)
    _ = await _call(shell, "run_command", command="mkdir -p sub")
    _ = await _call(shell, "run_command", command="cd sub")
    result = await _call(shell, "run_command", command="pwd")
    assert s(result["stdout"]).strip() == "/work/sub"
    _ = await _call(shell, "run_command", command="echo inside > here.txt")
    assert (repo / "sub" / "here.txt").read_text() == "inside\n"


async def test_a_cd_above_the_project_root_does_not_persist(repo: Path, copies: ProjectCopies):
    shell = _shell(repo, copies)
    _ = await _call(shell, "run_command", command="mkdir -p sub")
    _ = await _call(shell, "run_command", command="cd sub")
    _ = await _call(shell, "run_command", command="cd ../..")
    result = await _call(shell, "run_command", command="pwd")
    assert s(result["stdout"]).strip() == "/work/sub"


async def test_a_failed_cd_leaves_the_directory_where_it_was(repo: Path, copies: ProjectCopies):
    shell = _shell(repo, copies)
    result = await _call(shell, "run_command", command="cd nowhere-at-all")
    assert result["ok"] is False and result["exit_code"] != 0
    assert s((await _call(shell, "run_command", command="pwd"))["stdout"]).strip() == "/work"


async def test_a_workspace_command_gets_no_proxy_and_cannot_reach_out(
    repo: Path, copies: ProjectCopies, egress: EgressPolicy
):
    shell = _shell(repo, copies, egress)
    env = await _call(shell, "run_command", command="env")
    env_stdout = s(env["stdout"])
    assert "HTTP_PROXY" not in env_stdout and "http_proxy" not in env_stdout

    # The URL is assembled at run time: a command that names one is given the network.
    curl = await _call(
        shell, "run_command", command='U=ht""tp://example.com/; curl -sS --max-time 5 "$U"'
    )
    assert curl["exit_code"] not in (0, 127), curl["stderr"]

    script = (
        "import urllib.request; "
        "urllib.request.urlopen('ht' + 'tp://example.com/', timeout=5).read()"
    )
    urllib = await _call(shell, "run_command", command=f'python3 -c "{script}"')
    assert urllib["exit_code"] not in (0, 127), urllib["stderr"]
    assert "URLError" in s(urllib["stderr"])


async def test_a_network_command_gets_the_egress_proxy_with_credentials(
    repo: Path, copies: ProjectCopies, egress: EgressPolicy
):
    shell = _shell(repo, copies, egress)
    result = await _call(shell, "run_command", command="env", reach="network")
    env = {
        key: value
        for key, _, value in (line.partition("=") for line in s(result["stdout"]).splitlines())
    }
    assert result["reach"] == "network" and result["fenced"] is True
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        assert env[name].startswith("http://workspace:") and "@127.0.0.1:" in env[name]
    assert "localhost" in env["NO_PROXY"]


async def test_a_workspace_command_that_names_a_url_gets_the_network_it_needs(
    repo: Path, copies: ProjectCopies, egress: EgressPolicy
):
    shell = _shell(repo, copies, egress)
    result = await _call(shell, "run_command", command="echo https://example.com; env")
    assert result["reach"] == "workspace" and result["fenced"] is True
    assert "HTTP_PROXY=http://workspace:" in s(result["stdout"])


async def test_a_background_command_shows_output_and_is_stopped(repo: Path, copies: ProjectCopies):
    shell = _shell(repo, copies)
    started = await _call(shell, "start_command", command="echo up; sleep 60")
    command_id = s(started["command_id"])
    assert started["command"] == "echo up; sleep 60" and started["fenced"] is True

    checked = await _call(shell, "check_command", command_id=command_id)
    for _ in range(50):
        if "up" in s(checked["stdout"]):
            break
        await asyncio.sleep(0.1)
        checked = await _call(shell, "check_command", command_id=command_id)
    assert "up" in s(checked["stdout"]) and checked["status"] == "running"
    assert checked["exit_code"] is None

    stopped = await _call(shell, "stop_command", command_id=command_id)
    assert stopped["status"] == "stopped" and "up" in s(stopped["stdout"])
    again = await _call(shell, "stop_command", command_id=command_id)
    assert again["ok"] is False and command_id in s(again["error"])


async def test_a_finished_background_command_reports_its_exit_and_brings_its_work_back(
    repo: Path, copies: ProjectCopies
):
    shell = _shell(repo, copies)
    started = await _call(shell, "start_command", command="echo made > from-bg.txt; exit 3")
    command_id = s(started["command_id"])
    checked = await _call(shell, "check_command", command_id=command_id)
    for _ in range(50):
        if checked["status"] == "finished":
            break
        await asyncio.sleep(0.1)
        checked = await _call(shell, "check_command", command_id=command_id)
    assert checked["status"] == "finished" and checked["exit_code"] == 3
    assert checked["ok"] is False
    assert (repo / "from-bg.txt").read_text() == "made\n"


async def test_a_command_past_its_timeout_is_timed_out_with_no_exit_code(
    repo: Path, copies: ProjectCopies
):
    shell = _shell(repo, copies)
    result = await _call(
        shell, "run_command", command="echo reached-here; sleep 30", timeout_seconds=2
    )
    assert result["timed_out"] is True and result["exit_code"] is None
    assert result["ok"] is False and "reached-here" in s(result["stdout"])
    assert "terminated" in s(result["error"])


async def test_a_running_commands_output_reaches_the_operators_stream(
    repo: Path, copies: ProjectCopies, monkeypatch: pytest.MonkeyPatch
):
    frames: list[RunEventEmitted] = []

    async def emit(_self: RunContext[object], event: RunEventEmitted) -> RunEventEmitted:
        frames.append(event)
        return event

    monkeypatch.setattr(RunContext, "emit", emit)
    shell = _shell(repo, copies)
    shell.ctx.tool_call_id = "call-1"
    result = await _call(shell, "run_command", command="echo streaming; sleep 2; echo more")

    bodies = [frame.body for frame in frames if isinstance(frame.body, ToolProgress)]
    assert len(bodies) == len(frames)
    assert len(bodies) >= 2 and all(body.tool_call_id == "call-1" for body in bodies)
    assert all(body.elapsed_s is not None for body in bodies)
    streamed = "".join(body.partial or "" for body in bodies)
    assert streamed.count("streaming") == 1 and "more" in streamed
    first = bodies[0].partial or ""
    assert "streaming" in first and "more" not in first
    assert "streaming" in s(result["stdout"]) and "more" in s(result["stdout"])


async def test_a_commit_in_the_vm_lands_on_the_host_branch(repo: Path, copies: ProjectCopies):
    shell = _shell(repo, copies)
    result = await _call(
        shell, "run_command", command="echo more >> hello.txt && git commit -qam 'agent change'"
    )
    assert result["ok"] is True and "notes" not in result
    assert git(repo, "log", "-1", "--format=%s") == "agent change"
    assert git(repo, "status", "--porcelain", "--untracked-files=no") == ""


async def test_a_rewritten_branch_stays_in_the_vm_and_is_noted(repo: Path, copies: ProjectCopies):
    before = git(repo, "rev-parse", "HEAD")
    shell = _shell(repo, copies)
    result = await _call(shell, "run_command", command="git commit -q --amend -m rewritten")
    assert result["ok"] is True
    assert any("rewritten" in s(note) for note in arr(result["notes"]))
    assert git(repo, "rev-parse", "HEAD") == before


async def test_work_on_another_branch_is_noted_and_not_landed(repo: Path, copies: ProjectCopies):
    before = git(repo, "rev-parse", "HEAD")
    shell = _shell(repo, copies)
    result = await _call(
        shell,
        "run_command",
        command="git checkout -qb side && git commit -q --allow-empty -m side",
    )
    assert any("refs/heads/side" in s(note) for note in arr(result["notes"]))
    assert git(repo, "rev-parse", "HEAD") == before


async def test_a_change_made_on_the_host_reaches_the_next_command(
    repo: Path, copies: ProjectCopies
):
    shell = _shell(repo, copies)
    _ = await _call(shell, "run_command", command="true")
    _ = (repo / "hello.txt").write_text("operator edit\n")
    result = await _call(shell, "run_command", command="cat hello.txt")
    assert result["stdout"] == "operator edit\n"
