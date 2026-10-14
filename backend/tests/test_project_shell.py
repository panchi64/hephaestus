"""The project shell end to end: the `shell` tools, the project sync and the guest scripts.

Commands run in a platform VM whose guest is this machine (`vm_local`), against a temp git
repository. What is asserted is what the operator and the model can observe: files and
commits landing in the host repository, the working directory carried between commands,
the network a command is (not) given, and background commands.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from pydantic_ai import RunContext

from runs import Event, ToolProgress
from services.egress import EgressPolicy
from services.vm.project import ProjectCopies
from tools.shell import shell_toolset

from .conftest import egress_policy
from .shell_support import Shell, checkout_ctx, git, local_copies, make_repo
from .vm_local import can_confine

pytestmark = pytest.mark.skipif(not can_confine(), reason="needs root and bubblewrap")


@pytest.fixture
def repo(tmp_path) -> Path:
    return make_repo(tmp_path / "project")


@pytest.fixture
async def copies(tmp_path):
    async with local_copies(tmp_path / "vm") as live:
        yield live


@pytest.fixture
def egress(tmp_path) -> EgressPolicy:
    return egress_policy(tmp_path / "data", ("pypi.org",))


def _shell(repo: Path, copies: ProjectCopies, egress: EgressPolicy | None = None) -> Shell:
    return Shell(shell_toolset(), checkout_ctx(repo, copies=copies, egress=egress))


async def test_a_workspace_command_edits_a_file_and_it_lands_in_the_host_repo(repo, copies):
    shell = _shell(repo, copies)
    result = await shell.call(
        "run_command", command="echo changed > hello.txt && echo new > added.txt"
    )
    assert result["ok"] is True
    assert result["reach"] == "workspace" and result["fenced"] is True
    assert (repo / "hello.txt").read_text() == "changed\n"
    assert (repo / "added.txt").read_text() == "new\n"


async def test_a_command_runs_in_the_vm_copy_not_in_the_host_folder(repo, copies):
    shell = _shell(repo, copies)
    result = await shell.call("run_command", command="pwd; ls")
    assert result["stdout"].splitlines()[0] == "/work"
    assert str(repo) not in result["stdout"]
    assert "hello.txt" in result["stdout"]


async def test_a_cd_into_a_subdirectory_persists_to_the_next_command(repo, copies):
    shell = _shell(repo, copies)
    await shell.call("run_command", command="mkdir -p sub")
    await shell.call("run_command", command="cd sub")
    result = await shell.call("run_command", command="pwd")
    assert result["stdout"].strip() == "/work/sub"
    await shell.call("run_command", command="echo inside > here.txt")
    assert (repo / "sub" / "here.txt").read_text() == "inside\n"


async def test_a_cd_above_the_project_root_does_not_persist(repo, copies):
    shell = _shell(repo, copies)
    await shell.call("run_command", command="mkdir -p sub")
    await shell.call("run_command", command="cd sub")
    await shell.call("run_command", command="cd ../..")
    result = await shell.call("run_command", command="pwd")
    assert result["stdout"].strip() == "/work/sub"


async def test_a_failed_cd_leaves_the_directory_where_it_was(repo, copies):
    shell = _shell(repo, copies)
    result = await shell.call("run_command", command="cd nowhere-at-all")
    assert result["ok"] is False and result["exit_code"] != 0
    assert (await shell.call("run_command", command="pwd"))["stdout"].strip() == "/work"


async def test_a_workspace_command_gets_no_proxy_and_cannot_reach_out(repo, copies, egress):
    shell = _shell(repo, copies, egress)
    env = await shell.call("run_command", command="env")
    assert "HTTP_PROXY" not in env["stdout"] and "http_proxy" not in env["stdout"]

    # The URL is assembled at run time: a command that names one is given the network.
    curl = await shell.call(
        "run_command", command='U=ht""tp://example.com/; curl -sS --max-time 5 "$U"'
    )
    assert curl["exit_code"] not in (0, 127), curl["stderr"]

    script = (
        "import urllib.request; "
        "urllib.request.urlopen('ht' + 'tp://example.com/', timeout=5).read()"
    )
    urllib = await shell.call("run_command", command=f'python3 -c "{script}"')
    assert urllib["exit_code"] not in (0, 127), urllib["stderr"]
    assert "URLError" in urllib["stderr"]


async def test_a_network_command_gets_the_egress_proxy_with_credentials(repo, copies, egress):
    shell = _shell(repo, copies, egress)
    result = await shell.call("run_command", command="env", reach="network")
    env = dict(line.partition("=")[::2] for line in result["stdout"].splitlines())
    assert result["reach"] == "network" and result["fenced"] is True
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        assert env[name].startswith("http://workspace:") and "@127.0.0.1:" in env[name]
    assert "localhost" in env["NO_PROXY"]


async def test_a_workspace_command_that_names_a_url_gets_the_network_it_needs(repo, copies, egress):
    shell = _shell(repo, copies, egress)
    result = await shell.call("run_command", command="echo https://example.com; env")
    assert result["reach"] == "workspace" and result["fenced"] is True
    assert "HTTP_PROXY=http://workspace:" in result["stdout"]


async def test_a_background_command_shows_output_and_is_stopped(repo, copies):
    shell = _shell(repo, copies)
    started = await shell.call("start_command", command="echo up; sleep 60")
    command_id = started["command_id"]
    assert started["command"] == "echo up; sleep 60" and started["fenced"] is True

    checked = await shell.call("check_command", command_id=command_id)
    for _ in range(50):
        if "up" in checked["stdout"]:
            break
        await asyncio.sleep(0.1)
        checked = await shell.call("check_command", command_id=command_id)
    assert "up" in checked["stdout"] and checked["status"] == "running"
    assert checked["exit_code"] is None

    stopped = await shell.call("stop_command", command_id=command_id)
    assert stopped["status"] == "stopped" and "up" in stopped["stdout"]
    again = await shell.call("stop_command", command_id=command_id)
    assert again["ok"] is False and command_id in again["error"]


async def test_a_finished_background_command_reports_its_exit_and_brings_its_work_back(
    repo, copies
):
    shell = _shell(repo, copies)
    started = await shell.call("start_command", command="echo made > from-bg.txt; exit 3")
    checked = await shell.call("check_command", command_id=started["command_id"])
    for _ in range(50):
        if checked["status"] == "finished":
            break
        await asyncio.sleep(0.1)
        checked = await shell.call("check_command", command_id=started["command_id"])
    assert checked["status"] == "finished" and checked["exit_code"] == 3
    assert checked["ok"] is False
    assert (repo / "from-bg.txt").read_text() == "made\n"


async def test_a_command_past_its_timeout_is_timed_out_with_no_exit_code(repo, copies):
    shell = _shell(repo, copies)
    result = await shell.call(
        "run_command", command="echo reached-here; sleep 30", timeout_seconds=2
    )
    assert result["timed_out"] is True and result["exit_code"] is None
    assert result["ok"] is False and "reached-here" in result["stdout"]
    assert "terminated" in result["error"]


async def test_a_running_commands_output_reaches_the_operators_stream(repo, copies, monkeypatch):
    frames: list[Event] = []

    async def emit(_self, event):
        frames.append(event)
        return event

    monkeypatch.setattr(RunContext, "emit", emit)
    shell = _shell(repo, copies)
    shell.ctx.tool_call_id = "call-1"
    result = await shell.call("run_command", command="echo streaming; sleep 2; echo more")

    bodies = [frame.body for frame in frames if isinstance(frame.body, ToolProgress)]
    assert len(bodies) == len(frames)
    assert len(bodies) >= 2 and all(body.tool_call_id == "call-1" for body in bodies)
    assert all(body.elapsed_s is not None for body in bodies)
    streamed = "".join(body.partial or "" for body in bodies)
    assert streamed.count("streaming") == 1 and "more" in streamed
    first = bodies[0].partial or ""
    assert "streaming" in first and "more" not in first
    assert "streaming" in result["stdout"] and "more" in result["stdout"]


async def test_a_commit_in_the_vm_lands_on_the_host_branch(repo, copies):
    shell = _shell(repo, copies)
    result = await shell.call(
        "run_command", command="echo more >> hello.txt && git commit -qam 'agent change'"
    )
    assert result["ok"] is True and "notes" not in result
    assert git(repo, "log", "-1", "--format=%s") == "agent change"
    assert git(repo, "status", "--porcelain", "--untracked-files=no") == ""


async def test_a_rewritten_branch_stays_in_the_vm_and_is_noted(repo, copies):
    before = git(repo, "rev-parse", "HEAD")
    shell = _shell(repo, copies)
    result = await shell.call("run_command", command="git commit -q --amend -m rewritten")
    assert result["ok"] is True
    assert any("rewritten" in note for note in result["notes"])
    assert git(repo, "rev-parse", "HEAD") == before


async def test_work_on_another_branch_is_noted_and_not_landed(repo, copies):
    before = git(repo, "rev-parse", "HEAD")
    shell = _shell(repo, copies)
    result = await shell.call(
        "run_command", command="git checkout -qb side && git commit -q --allow-empty -m side"
    )
    assert any("refs/heads/side" in note for note in result["notes"])
    assert git(repo, "rev-parse", "HEAD") == before


async def test_a_change_made_on_the_host_reaches_the_next_command(repo, copies):
    shell = _shell(repo, copies)
    await shell.call("run_command", command="true")
    (repo / "hello.txt").write_text("operator edit\n")
    result = await shell.call("run_command", command="cat hello.txt")
    assert result["stdout"] == "operator edit\n"
