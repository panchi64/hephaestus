"""How a fence's refusal is classified, and the payload a host command returns."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import JsonValue, TypeAdapter
from pydantic_ai import RunContext
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage

from core.config import Settings
from core.container import ServiceContainer
from runs import Run, RunStream
from services.sandbox import HostConfinement, SandboxResult
from services.sandbox.refusals import denied_host, host_refusal, is_egress_denied, vm_refusal
from tools import RunDeps
from tools import shell as shell_module

from ._json import s

HOME = Path("/Users/tester")

PIP_TUNNEL = (
    "ProxyError('Cannot connect to proxy.', OSError('Tunnel connection failed: 403 Forbidden')): "
    "HTTPSConnectionPool(host='files.example.org', port=443)"
)
CURL_TUNNEL = "curl: (56) CONNECT tunnel failed, response 403\ncurl: https://api.example.net/v1"
DNS_FAILURE = "curl: (6) Could not resolve host: registry.example.io"
NO_HOST = "Temporary failure in name resolution"


class TestVMRefusals:
    def test_a_name_the_vm_could_not_resolve_is_the_host_it_refused(self):
        refusal = vm_refusal(ok=False, stdout="", stderr=DNS_FAILURE)
        assert refusal is not None
        assert refusal.denied_host == "registry.example.io"
        assert "code_request_egress" in refusal.note

    def test_a_refused_https_tunnel_names_the_host_the_client_was_reaching(self):
        pip = vm_refusal(ok=False, stdout="", stderr=PIP_TUNNEL)
        curl = vm_refusal(ok=False, stdout="", stderr=CURL_TUNNEL)
        assert pip is not None and pip.denied_host == "files.example.org"
        assert curl is not None and curl.denied_host == "api.example.net"

    def test_a_failure_that_names_no_host_still_points_at_the_egress_request(self):
        refusal = vm_refusal(ok=False, stdout="", stderr=NO_HOST)
        assert refusal is not None
        assert refusal.denied_host is None and "code_request_egress" in refusal.note

    def test_a_command_that_succeeded_gets_no_note_for_text_it_printed(self):
        assert vm_refusal(ok=True, stdout="", stderr=DNS_FAILURE) is None

    def test_a_read_only_project_is_reported(self):
        refusal = vm_refusal(ok=False, stdout="", stderr="touch: Read-only file system")
        assert refusal is not None and "read-only" in refusal.note

    @pytest.mark.parametrize(
        "stderr",
        [
            "Traceback (most recent call last):\nFileNotFoundError: [Errno 2] No such file: 'x'",
            "ModuleNotFoundError: No module named 'numpy'",
        ],
    )
    def test_an_ordinary_failure_is_not_a_refusal(self, stderr: str):
        assert vm_refusal(ok=False, stdout="", stderr=stderr) is None
        assert not is_egress_denied(stderr)

    def test_the_denied_host_needs_a_resolution_failure_to_name_it(self):
        assert denied_host("curl: (7) Failed to connect to example.com port 443") is None
        assert denied_host(DNS_FAILURE) == "registry.example.io"


class TestHostRefusals:
    def test_a_write_to_a_floor_path_is_named_with_its_reason(self):
        stderr = "/bin/zsh: /Users/tester/.zshrc: Operation not permitted"
        refusal = host_refusal(fence_active=True, ok=False, stderr=stderr, home=HOME)
        assert refusal is not None
        assert "/Users/tester/.zshrc" in refusal.note
        assert "protected-write floor" in refusal.note
        assert refusal.denied_host is None

    def test_a_floor_path_is_reported_even_when_the_command_exited_zero(self):
        stderr = "tee: /Users/tester/.claude/x: Operation not permitted"
        refusal = host_refusal(fence_active=True, ok=True, stderr=stderr, home=HOME)
        assert refusal is not None and ".claude" in refusal.note

    def test_a_bubblewrap_denial_elsewhere_reads_as_the_fence_when_the_command_failed(self):
        stderr = "mkdir: cannot create directory '/opt/x': Permission denied"
        refusal = host_refusal(fence_active=True, ok=False, stderr=stderr, home=HOME)
        assert refusal is not None
        assert "outside" in refusal.note or "host fence" in refusal.note

    def test_a_permission_message_in_a_successful_command_is_not_a_refusal(self):
        stderr = "find: '/private/var/db': Operation not permitted"
        assert host_refusal(fence_active=True, ok=True, stderr=stderr, home=HOME) is None

    def test_an_ssh_private_key_gets_the_agent_instruction(self, tmp_path: Path):
        (tmp_path / ".ssh").mkdir()
        _ = (tmp_path / ".ssh" / "id_ed25519").write_text("key")
        stderr = f"cat: {tmp_path}/.ssh/id_ed25519: Operation not permitted"
        refusal = host_refusal(fence_active=True, ok=False, stderr=stderr, home=tmp_path)
        assert refusal is not None
        assert refusal.note == (
            "the SSH private key is not readable inside the host fence; ask the operator to "
            "load it into ssh-agent once (`ssh-add --apple-use-keychain <key>` on macOS) so "
            "git can sign through the agent."
        )

    def test_another_credential_file_points_at_the_credential_helper(self, tmp_path: Path):
        stderr = f"cat: {tmp_path}/.aws/credentials: Operation not permitted"
        refusal = host_refusal(fence_active=True, ok=False, stderr=stderr, home=tmp_path)
        assert refusal is not None
        assert refusal.note.startswith("the AWS credentials file is not readable")
        assert "credential helper or keychain login" in refusal.note

    def test_nothing_is_attributed_to_a_fence_that_was_not_applied(self):
        stderr = "/bin/zsh: /Users/tester/.zshrc: Operation not permitted"
        assert host_refusal(fence_active=False, ok=False, stderr=stderr, home=HOME) is None

    @pytest.mark.parametrize(
        "stderr",
        [
            "Traceback (most recent call last):\nFileNotFoundError: [Errno 2] No such file: 'x'",
            "cat: /Users/tester/missing: No such file or directory",
            "",
        ],
    )
    def test_a_missing_file_is_not_a_refusal(self, stderr: str):
        assert host_refusal(fence_active=True, ok=False, stderr=stderr, home=HOME) is None


def _deps() -> RunDeps:
    run = Run(id="run-1", kind="chat", owner_id="operator", stream=RunStream())
    return RunDeps(run=run, owner_id="operator", caps=ServiceContainer(), conversation_id="conv-1")


async def _host_payload(
    monkeypatch: pytest.MonkeyPatch, confinement: HostConfinement, result: SandboxResult
) -> dict[str, JsonValue]:
    async def resolved(_settings: Settings) -> HostConfinement:
        return confinement

    async def ran(_command: str, **_kwargs: object) -> SandboxResult:
        return result

    monkeypatch.setattr(shell_module, "run_on_host", ran)
    monkeypatch.setattr(shell_module, "resolve_confinement", resolved)
    monkeypatch.setattr(shell_module, "host_scratch_dir", lambda: Path("/tmp"))
    ctx = RunContext(deps=_deps(), model=TestModel(), usage=RunUsage())
    return TypeAdapter(dict[str, JsonValue]).validate_python(
        await shell_module.run_on_host_tool(ctx, "x", "x")
    )


async def test_a_fenced_host_command_carries_the_shells_fields(monkeypatch: pytest.MonkeyPatch):
    payload = await _host_payload(
        monkeypatch,
        HostConfinement(True),
        SandboxResult(
            exit_code=1, stdout="", stderr="touch: /Users/t/.zshrc: Operation not permitted"
        ),
    )
    assert payload["fenced"] is True
    assert "unfenced_reason" not in payload
    assert "confined" not in payload and "confinement_note" not in payload
    assert "fence_note" in payload


async def test_an_unfenced_host_command_says_why(monkeypatch: pytest.MonkeyPatch):
    payload = await _host_payload(
        monkeypatch,
        HostConfinement(False, reason="bubblewrap is missing"),
        SandboxResult(exit_code=0, stdout="hi", stderr=""),
    )
    assert payload["fenced"] is False
    assert "bubblewrap is missing" in s(payload["unfenced_reason"])
    assert "fence_note" not in payload
