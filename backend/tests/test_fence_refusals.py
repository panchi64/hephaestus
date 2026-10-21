"""How a fence's refusal is classified, and the payload a host command returns."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic_ai import RunContext
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage

from core.config import Settings
from core.container import ServiceContainer
from runs import Run, RunStream
from services.sandbox import HostConfinement, SandboxResult
from services.sandbox.refusals import denied_host, host_refusal, is_egress_denied, vm_refusal
from tools import RunDeps
from tools import code as code_module
from tools.code import code_toolset

from ._json import s

HOME = Path("/Users/tester")

EGRESS_403 = "HTTP/1.1 403 Forbidden\nhephaestus-egress: denied evil.example.com\n"
PIP_TUNNEL = (
    "ProxyError('Cannot connect to proxy.', OSError('Tunnel connection failed: 403 Forbidden')): "
    "HTTPSConnectionPool(host='files.example.org', port=443)"
)
CURL_TUNNEL = "curl: (56) CONNECT tunnel failed, response 403\ncurl: https://api.example.net/v1"
DNS_FAILURE = "curl: (6) Could not resolve host: registry.example.io"
OFFLINE = "Temporary failure in name resolution"


class TestVMRefusals:
    def test_a_gateway_refusal_names_the_host_it_refused(self):
        refusal = vm_refusal(network=True, ok=False, stdout=EGRESS_403, stderr="")
        assert refusal is not None
        assert refusal.denied_host == "evil.example.com"
        assert "code_request_egress" in refusal.note

    def test_a_refused_https_tunnel_names_the_host_the_client_was_reaching(self):
        pip = vm_refusal(network=True, ok=False, stdout="", stderr=PIP_TUNNEL)
        curl = vm_refusal(network=True, ok=False, stdout="", stderr=CURL_TUNNEL)
        assert pip is not None and pip.denied_host == "files.example.org"
        assert curl is not None and curl.denied_host == "api.example.net"

    def test_an_offline_dns_failure_is_told_to_redeclare_and_names_no_host(self):
        refusal = vm_refusal(network=False, ok=False, stdout="", stderr=OFFLINE)
        assert refusal is not None
        assert refusal.denied_host is None and "reach `network`" in refusal.note

    def test_a_networked_dns_failure_is_not_a_refusal(self):
        assert vm_refusal(network=True, ok=False, stdout="", stderr=DNS_FAILURE) is None

    def test_a_read_only_project_is_reported(self):
        refusal = vm_refusal(
            network=False, ok=False, stdout="", stderr="touch: Read-only file system"
        )
        assert refusal is not None and "read-only" in refusal.note

    @pytest.mark.parametrize(
        "stderr",
        [
            "Traceback (most recent call last):\nFileNotFoundError: [Errno 2] No such file: 'x'",
            "ModuleNotFoundError: No module named 'numpy'",
        ],
    )
    def test_an_ordinary_failure_is_not_a_refusal(self, stderr: str):
        assert vm_refusal(network=False, ok=False, stdout="", stderr=stderr) is None
        assert vm_refusal(network=True, ok=False, stdout="", stderr=stderr) is None
        assert not is_egress_denied(stderr)

    def test_the_denied_host_needs_the_gateways_own_words(self):
        assert denied_host("curl: (7) Failed to connect to example.com port 443") is None
        assert denied_host(EGRESS_403) == "evil.example.com"


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
) -> dict[str, object]:
    async def resolved(_settings: Settings) -> HostConfinement:
        return confinement

    async def ran(_command: str, **_kwargs: object) -> SandboxResult:
        return result

    monkeypatch.setattr("tools.code.run_on_host", ran)
    monkeypatch.setattr("tools.code.resolve_confinement", resolved)
    monkeypatch.setattr(code_module, "host_scratch_dir", lambda: Path("/tmp"))
    ctx = RunContext(deps=_deps(), model=TestModel(), usage=RunUsage())
    toolset = code_toolset()
    tools = await toolset.get_tools(ctx)
    out = await toolset.call_tool(
        "run_host_command", {"command": "x", "explanation": "x"}, ctx, tools["run_host_command"]
    )
    assert isinstance(out, dict)
    return {str(key): value for key, value in out.items()}


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
