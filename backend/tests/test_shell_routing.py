"""Where `tools/shell.py` runs a command: the declared reach picks the VM or the host."""

from __future__ import annotations

from pathlib import Path

import pytest

from services.home.backend import Mount
from services.home.paths import SCRATCH
from services.home.table import RunHome
from services.permissions import Reach
from services.sandbox.shell_runner import ShellResult
from tools import shell as shell_tool


def _failed(stderr: str) -> ShellResult:
    return ShellResult(exit_code=1, stdout="", stderr=stderr, timed_out=False, duration_ms=5)


class TestWhereTheToolRunsACommand:
    """`tools/shell.py`'s own choice: the *declaration* picks where a command runs, and only
    `host` leaves the platform VM — an explicit yes that the approver was shown.

    The availability of the VM is deliberately not one of the inputs here. A chat with no
    running VM refuses the shell tools outright, so by the time this function runs there
    is always a VM to route to — which is what keeps "cleared or not, the command runs in
    the VM" a statement with no exceptions in it.
    """

    def _boundary(self, tmp_path: Path, command: str, *, reach: Reach):
        home = RunHome(mounts=(Mount(SCRATCH, tmp_path, writable=True),), default=SCRATCH, key="t")
        boundary = shell_tool.boundary("run_command", command, reach, home, SCRATCH.path)
        assert boundary.reach == reach
        assert boundary.fenced is (boundary.where == "vm")
        assert (boundary.reason is None) is boundary.fenced
        return boundary

    def test_an_offline_command_runs_in_the_vm_with_no_network(self, tmp_path: Path):
        boundary = self._boundary(tmp_path, "uv run pytest", reach="offline")
        assert boundary.where == "vm" and boundary.network is False

    @pytest.mark.parametrize(
        "command",
        ["pip install requests", "uv sync", "npm install", "git -C src fetch origin"],
    )
    def test_an_offline_install_or_clone_is_given_the_network_it_needs(
        self, tmp_path: Path, command: str
    ):
        boundary = self._boundary(tmp_path, command, reach="offline")
        assert boundary.where == "vm" and boundary.network is True

    def test_a_network_command_runs_in_the_vm_with_the_conversations_egress(self, tmp_path: Path):
        boundary = self._boundary(tmp_path, "uv sync", reach="network")
        assert boundary.where == "vm" and boundary.network is True

    def test_an_offline_command_that_names_a_url_is_given_the_network_it_needs(
        self, tmp_path: Path
    ):
        # It was approved in the words it was read in, so it gets what it plainly asks for
        # rather than failing; the declaration itself is reported as made.
        boundary = self._boundary(tmp_path, "curl https://example.com/x", reach="offline")
        assert boundary.where == "vm" and boundary.network is True

    @pytest.mark.parametrize(
        "stderr",
        [
            "Temporary failure in name resolution",
            "curl: (6) Could not resolve host: example.com",
            "npm ERR! getaddrinfo EAI_AGAIN registry.npmjs.org",
        ],
    )
    def test_a_command_with_no_network_that_tried_to_reach_one_is_told_to_redeclare(
        self, tmp_path: Path, stderr: str
    ):
        offline = self._boundary(tmp_path, "python fetch.py", reach="offline")
        note = shell_tool.fence_note(offline, _failed(stderr))
        assert note is not None and "reach `network`" in note and "`host`" in note

    def test_a_networked_command_that_could_not_resolve_gets_no_redeclare_note(
        self, tmp_path: Path
    ):
        networked = self._boundary(tmp_path, "python fetch.py", reach="network")
        failed = _failed("Temporary failure in name resolution")
        assert shell_tool.fence_note(networked, failed) is None

    def test_a_host_declaration_is_the_one_that_asks_for_the_machine(self, tmp_path: Path):
        boundary = self._boundary(tmp_path, "uv sync", reach="host")
        assert boundary.where == "host" and boundary.network is False
        assert boundary.reason is not None and "host" in boundary.reason
