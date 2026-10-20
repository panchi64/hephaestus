"""Where `tools/shell.py` runs a command: the declared reach picks the VM or the host."""

from __future__ import annotations

from pathlib import Path

import pytest

from services.home.backend import Mount
from services.home.paths import SCRATCH
from services.home.table import RunHome
from services.permissions import Reach
from tools import shell as shell_tool


def _note(stderr: str, *, network: bool = False, ok: bool = False) -> str | None:
    return shell_tool.fence_note(network=network, ok=ok, stdout="", stderr=stderr)


class TestWhatAFailedCommandIsTold:
    """The `fence_note` a VM command's result carries when the fence, not the command, is
    why it failed."""

    @pytest.mark.parametrize(
        "stderr",
        [
            "Temporary failure in name resolution",
            "curl: (6) Could not resolve host: example.com",
            "npm ERR! getaddrinfo EAI_AGAIN registry.npmjs.org",
            "dial tcp: lookup proxy.golang.org: no such host",
            "curl: (56) CONNECT tunnel failed, response 407",
            "Tunnel connection failed: 407 Proxy Authentication Required",
        ],
    )
    def test_an_offline_command_that_tried_to_reach_out_is_told_to_redeclare(self, stderr: str):
        note = _note(stderr)
        assert note is not None and "reach `network`" in note and "`host`" in note

    def test_a_command_that_succeeded_gets_no_note_for_text_it_printed(self):
        assert _note("ENOTFOUND matched in 3 files", ok=True) is None

    def test_a_networked_command_that_could_not_resolve_gets_no_redeclare_note(self):
        assert _note("Temporary failure in name resolution", network=True) is None

    def test_a_networked_command_refused_by_the_proxy_is_told_about_its_credentials(self):
        note = _note("407 Proxy Authentication Required", network=True)
        assert note is not None and "credentials" in note and "reach `network`" not in note


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

    def test_a_host_declaration_is_the_one_that_asks_for_the_machine(self, tmp_path: Path):
        boundary = self._boundary(tmp_path, "uv sync", reach="host")
        assert boundary.where == "host" and boundary.network is False
        assert boundary.reason is not None and "host" in boundary.reason
