"""Live-preview lifecycle on the session manager: token indexing, replacement,
active-view warmth, explicit stop, and teardown on reap/shutdown. The VM is a
:class:`~tests.vm_fakes.FakeVM`; the preview's host port points at a local HTTP stub."""

from __future__ import annotations

import asyncio
import socket

import pytest

from core.config import Settings
from core.vault import Vault
from services.sandbox import PreviewHandle, SandboxSessionManager, SandboxSpec
from services.sandbox.base import SandboxError, safe_key
from services.vm.driver import VMError

from .conftest import egress_policy
from .vm_fakes import FakePreview, FakeVM

_EXCLUDES = Settings().sandbox_walk_excludes


async def _vault(tmp_path) -> Vault:
    vault = Vault(tmp_path / "keyfile.json")
    await vault.setup("pw")
    return vault


def _manager(
    tmp_path,
    vault: Vault,
    vm: FakeVM,
    *,
    idle_ttl_s: float = 1800.0,
    preview_startup_timeout_s: float = 2.0,
) -> SandboxSessionManager:
    return SandboxSessionManager(
        vm,
        vault,
        egress=egress_policy(tmp_path),
        data_dir=tmp_path,
        idle_ttl_s=idle_ttl_s,
        reap_interval_s=60.0,
        excludes=_EXCLUDES,
        preview_startup_timeout_s=preview_startup_timeout_s,
    )


def _forward(handle: PreviewHandle) -> FakePreview:
    assert isinstance(handle.forward, FakePreview)
    return handle.forward


async def _answer(reader, writer):
    await reader.read(4096)
    writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
    await writer.drain()
    writer.close()


@pytest.fixture
async def vm(tmp_path):
    """A VM whose previews are reached on a port that answers HTTP."""
    fake = FakeVM(tmp_path / "sandbox" / "work")
    server = await asyncio.start_server(_answer, "127.0.0.1", 0)
    fake.preview_port = server.sockets[0].getsockname()[1]
    async with server:
        yield fake


async def test_start_preview_indexes_token_and_resolves(tmp_path, vm):
    mgr = _manager(tmp_path, await _vault(tmp_path), vm)
    handle = await mgr.start_preview("conv-a", ["python", "-m", "http.server", "8000"], 8000)

    assert handle.path == f"/previews/{handle.token}/"
    assert handle.host_port == vm.preview_port
    assert handle.server_port == 8000
    assert mgr.resolve_preview(handle.token) is handle
    assert mgr.resolve_preview("not-a-real-token") is None


async def test_the_server_runs_confined_behind_the_same_proxy_env_as_a_command(tmp_path, vm):
    # A preview is not a hole in the fence: it is a job in the conversation's own
    # workspace, with the same credentials, published through the preview socket.
    mgr = _manager(tmp_path, await _vault(tmp_path), vm)
    await mgr.start_preview("conv-a", ["srv"], 8000)

    job = vm.background[f"preview-{safe_key('conv-a')}"]
    assert job["key"] == safe_key("conv-a")
    assert job["argv"] == ["srv"]
    assert job["publish"] == 8000
    assert job["env"]["HTTPS_PROXY"].startswith("http://workspace:")
    assert vm.events.index(("push", safe_key("conv-a"))) < vm.events.index(
        ("start_job", f"preview-{safe_key('conv-a')}")
    )


async def test_a_server_that_never_serves_reports_its_log(tmp_path, vm):
    # The preview port answers nothing, and the process has exited: the agent gets the
    # server's own output rather than a healthy-looking handle for a dead command.
    vm.preview_port = _unused_port()
    vm.alive_after_start = False
    vm.log = "npm ERR! missing script: dev"
    mgr = _manager(tmp_path, await _vault(tmp_path), vm, preview_startup_timeout_s=0.3)

    with pytest.raises(SandboxError, match="missing script"):
        await mgr.start_preview("conv-a", ["npm", "run", "dev"], 8000)

    assert vm.previews[0].closed
    session = mgr.existing("conv-a")
    assert session is not None
    assert session.preview is None


async def test_a_slow_server_that_is_still_running_is_opened_anyway(tmp_path, vm):
    vm.preview_port = _unused_port()
    mgr = _manager(tmp_path, await _vault(tmp_path), vm, preview_startup_timeout_s=0.3)

    handle = await mgr.start_preview("conv-a", ["slow"], 8000)

    assert mgr.resolve_preview(handle.token) is handle
    assert not vm.previews[0].closed


async def test_a_vm_that_cannot_start_the_job_is_a_sandbox_error(tmp_path, vm):
    mgr = _manager(tmp_path, await _vault(tmp_path), vm)
    session = await mgr.acquire("conv-a")
    await session.run(SandboxSpec(command=["true"]))
    vm.fail_with = VMError("no room")

    with pytest.raises(SandboxError, match="failed to start preview server"):
        await mgr.start_preview("conv-a", ["srv"], 8000)


async def test_starting_a_second_preview_replaces_the_first(tmp_path, vm):
    mgr = _manager(tmp_path, await _vault(tmp_path), vm)
    first = await mgr.start_preview("conv-a", ["one"], 8000)
    first_forward = _forward(first)
    second = await mgr.start_preview("conv-a", ["two"], 8000)

    assert mgr.resolve_preview(first.token) is None  # the old token no longer resolves
    assert mgr.resolve_preview(second.token) is second
    assert first_forward.closed  # and its forward was closed
    assert ("stop_job", f"preview-{safe_key('conv-a')}") in vm.events


async def test_resolve_preview_keeps_the_session_warm(tmp_path, vm):
    mgr = _manager(tmp_path, await _vault(tmp_path), vm)
    handle = await mgr.start_preview("conv-a", ["srv"], 8000)
    session = mgr._sessions[safe_key("conv-a")]

    session._last_used = 0.0  # pretend it went idle
    mgr.resolve_preview(handle.token)
    assert session._last_used > 0.0  # a proxied request refreshed it


async def test_stop_preview_deindexes_and_tears_down(tmp_path, vm):
    mgr = _manager(tmp_path, await _vault(tmp_path), vm)
    handle = await mgr.start_preview("conv-a", ["srv"], 8000)

    await mgr.stop_preview("conv-a")

    assert mgr.resolve_preview(handle.token) is None
    assert _forward(handle).closed
    assert not vm.background[f"preview-{safe_key('conv-a')}"]["running"]
    assert mgr._sessions  # the exec session itself survives an explicit stop


async def test_reaping_a_session_stops_its_preview_and_drops_the_token(tmp_path, vm):
    mgr = _manager(tmp_path, await _vault(tmp_path), vm, idle_ttl_s=0.0)
    handle = await mgr.start_preview("conv-a", ["srv"], 8000)

    await mgr._sweep()  # idle past TTL → reaped

    assert not mgr._sessions
    assert mgr.resolve_preview(handle.token) is None
    assert mgr.preview_status(handle.token) == "stopped"
    assert _forward(handle).closed
    assert not vm.background[f"preview-{safe_key('conv-a')}"]["running"]


async def test_stop_clears_all_previews(tmp_path, vm):
    mgr = _manager(tmp_path, await _vault(tmp_path), vm)
    handle = await mgr.start_preview("conv-a", ["srv"], 8000)

    await mgr.stop()

    assert mgr.resolve_preview(handle.token) is None
    assert _forward(handle).closed


def _unused_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port
