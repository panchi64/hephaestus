"""Where `tools/shell.py` runs a command: the declared reach picks the VM or the host."""

from __future__ import annotations

BRANCH = "odysseus/thread-1"


class TestWhereTheToolRunsACommand:
    """`tools/shell.py`'s own choice: the *declaration* picks where a command runs, and only
    `host` leaves the platform VM — an explicit yes that the approver was shown.

    The availability of the VM is deliberately not one of the inputs here. A chat with no
    running VM refuses the shell tools outright (`_guard`), so by the time this function
    runs there is always a VM to route to — which is what keeps "cleared or not, the
    command runs in the VM" a statement with no exceptions in it.
    """

    def _boundary(self, tmp_path, command, *, reach):
        from services.workspace import HostFiles, RunWorkspace
        from tools import shell as shell_tool

        workspace = RunWorkspace(
            root=tmp_path, kind="worktree", files=HostFiles(tmp_path), branch=BRANCH
        )
        boundary = shell_tool._boundary("run_command", command, reach, workspace)
        assert boundary.reach == reach
        assert boundary.fenced is (boundary.where == "vm")
        assert (boundary.reason is None) is boundary.fenced
        return boundary

    def test_a_workspace_command_runs_in_the_vm_with_no_network(self, tmp_path):
        boundary = self._boundary(tmp_path, "uv sync", reach="workspace")
        assert boundary.where == "vm" and boundary.network is False

    def test_a_network_command_runs_in_the_vm_with_the_conversations_egress(self, tmp_path):
        boundary = self._boundary(tmp_path, "uv sync", reach="network")
        assert boundary.where == "vm" and boundary.network is True

    def test_a_workspace_command_that_names_a_url_is_given_the_network_it_needs(self, tmp_path):
        # It was approved in the words it was read in, so it gets what it plainly asks for
        # rather than failing; the declaration itself is reported as made.
        boundary = self._boundary(tmp_path, "curl https://example.com/x", reach="workspace")
        assert boundary.where == "vm" and boundary.network is True

    def test_a_host_declaration_is_the_one_that_asks_for_the_machine(self, tmp_path):
        boundary = self._boundary(tmp_path, "uv sync", reach="host")
        assert boundary.where == "host" and boundary.network is False
        assert boundary.reason is not None and "host" in boundary.reason
