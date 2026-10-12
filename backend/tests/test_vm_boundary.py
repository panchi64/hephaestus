"""The platform VM's boundary, exercised with the real guest scripts on this machine.

What runs here is everything but the hypervisor: the confinement each command gets, the
egress tunnel and gateway, the preview mux, and moving a workspace in and out.
"""

from __future__ import annotations

import asyncio
import io
import json
import tarfile
from pathlib import Path

import pytest

import services.vm.egress as egress_mod
from services.vm.driver import VMError
from services.vm.egress import ALLOW_FILE, DENIED_MARKER
from services.vm.platform import WORKSPACE_PROXY
from services.vm.sync import unpack
from services.vm.workspace import GuestWorkspace

from .vm_local import can_confine, local_vm

confined = pytest.mark.skipif(not can_confine(), reason="needs root and bubblewrap")

_FETCH = """
import os, sys, urllib.request, urllib.error
try:
    with urllib.request.urlopen(sys.argv[1], timeout=10) as reply:
        print(reply.status, reply.read().decode())
except urllib.error.HTTPError as exc:
    print(exc.code, exc.read().decode())
"""


@pytest.fixture
async def vm(tmp_path):
    platform, _driver = await local_vm(tmp_path)
    yield platform
    await platform.stop()


def _proxy_env(credentials: str | None) -> dict[str, str]:
    auth = f"{credentials}@" if credentials else ""
    return {"HTTP_PROXY": f"http://{auth}{WORKSPACE_PROXY}"}


@confined
async def test_a_command_sees_only_its_own_workspace(vm, tmp_path):
    mine = GuestWorkspace(vm, "s-mine", tmp_path / "mine")
    theirs = GuestWorkspace(vm, "s-theirs", tmp_path / "theirs")
    (tmp_path / "theirs").mkdir()
    (tmp_path / "theirs" / "secret.txt").write_text("not yours")
    await theirs.bring_in()
    await mine.bring_in()

    result = await vm.run(
        "s-mine",
        ["sh", "-c", "id -u; pwd; ls -a /work; ls /; cat /work/../work/secret.txt"],
        env={},
        timeout_s=20,
    )
    lines = result.stdout.splitlines()
    assert lines[0] == "1100"
    assert lines[1] == "/work"
    assert "secret.txt" not in result.stdout
    assert "No such file" in result.stderr


@confined
async def test_a_command_can_use_its_own_tmp(vm, tmp_path):
    await GuestWorkspace(vm, "s-tmp", tmp_path / "ws").bring_in()
    result = await vm.run(
        "s-tmp", ["sh", "-c", "echo ok > /tmp/scratch && cat /tmp/scratch"], env={}, timeout_s=20
    )
    assert result.exit_code == 0, result.stderr
    assert result.stdout.strip() == "ok"


@confined
async def test_files_cross_both_ways_and_deletions_follow(vm, tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    (root / "input.txt").write_text("from the host")
    (root / "stale.txt").write_text("delete me")
    workspace = GuestWorkspace(vm, "s-files", root)
    await workspace.bring_in()

    await vm.run(
        "s-files",
        ["sh", "-c", "cat input.txt > copy.txt; mkdir -p out; echo built > out/a; rm stale.txt"],
        env={},
        timeout_s=20,
    )
    await workspace.bring_out()
    assert (root / "copy.txt").read_text() == "from the host"
    assert (root / "out" / "a").read_text() == "built\n"
    assert not (root / "stale.txt").exists()

    (root / "input.txt").write_text("edited on the host")
    (root / "copy.txt").unlink()
    await workspace.bring_in()
    result = await vm.run("s-files", ["sh", "-c", "cat input.txt; ls"], env={}, timeout_s=20)
    assert result.stdout.startswith("edited on the host")
    assert "copy.txt" not in result.stdout


@confined
async def test_an_idle_workspace_is_wiped_from_the_guest_and_restored_intact(vm, tmp_path):
    root = tmp_path / "ws"
    workspace = GuestWorkspace(vm, "s-idle", root)
    await workspace.bring_in()
    await vm.run("s-idle", ["sh", "-c", "echo kept > notes.md"], env={}, timeout_s=20)

    await workspace.evict()
    assert not Path(vm.layout.work("s-idle")).exists()
    assert (root / "notes.md").read_text() == "kept\n"

    await workspace.bring_in()
    result = await vm.run("s-idle", ["cat", "notes.md"], env={}, timeout_s=20)
    assert result.stdout == "kept\n"


@confined
async def test_a_command_past_its_timeout_is_stopped(vm, tmp_path):
    await GuestWorkspace(vm, "s-slow", tmp_path / "ws").bring_in()
    result = await vm.run("s-slow", ["sleep", "30"], env={}, timeout_s=1)
    assert result.exit_code == 124


@confined
async def test_stdin_reaches_the_command(vm, tmp_path):
    await GuestWorkspace(vm, "s-in", tmp_path / "ws").bring_in()
    result = await vm.run("s-in", ["cat"], env={}, timeout_s=20, stdin="piped")
    assert result.stdout == "piped"


@confined
async def test_a_command_cannot_reach_the_relay_or_the_host(vm, tmp_path):
    await GuestWorkspace(vm, "s-net", tmp_path / "ws").bring_in()
    probe = (
        "import socket, sys\n"
        "for port in map(int, sys.argv[1:]):\n"
        "    try:\n"
        "        socket.create_connection(('127.0.0.1', port), timeout=2)\n"
        "        print('open', port)\n"
        "    except OSError:\n"
        "        print('closed', port)\n"
    )
    layout = vm.layout
    result = await vm.run(
        "s-net",
        ["python3", "-c", probe, str(layout.pool_port), str(layout.mux_port), "22"],
        env={},
        timeout_s=20,
    )
    assert "open" not in result.stdout.replace("closed", "")


@confined
async def test_egress_needs_the_workspaces_credentials(vm, tmp_path):
    await GuestWorkspace(vm, "s-egress", tmp_path / "ws").bring_in()
    result = await vm.run(
        "s-egress",
        ["python3", "-c", _FETCH, "http://example.com/"],
        env=_proxy_env(None),
        timeout_s=20,
    )
    assert result.stdout.startswith("407")


@confined
async def test_egress_reaches_only_what_the_allowlist_names(vm, tmp_path, monkeypatch):
    allow_dir = tmp_path / "allow"
    allow_dir.mkdir()
    (allow_dir / ALLOW_FILE).write_text("allowed.test\n")
    credentials = vm.gateway.grant(allow_dir)

    async def upstream(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 7\r\nConnection: close\r\n\r\nupgrade")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(upstream, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]

    async def dial(_host, _port, _client):
        return await asyncio.open_connection("127.0.0.1", port)

    monkeypatch.setattr(egress_mod, "dial_pinned", dial)
    await GuestWorkspace(vm, "s-allow", tmp_path / "ws").bring_in()
    async with server:
        allowed = await vm.run(
            "s-allow",
            ["python3", "-c", _FETCH, "http://allowed.test/"],
            env=_proxy_env(credentials),
            timeout_s=20,
        )
        denied = await vm.run(
            "s-allow",
            ["python3", "-c", _FETCH, "http://elsewhere.test/"],
            env=_proxy_env(credentials),
            timeout_s=20,
        )
    assert allowed.stdout.strip() == "200 upgrade"
    assert denied.stdout.startswith("403") and DENIED_MARKER in denied.stdout

    vm.gateway.revoke(credentials)
    revoked = await vm.run(
        "s-allow",
        ["python3", "-c", _FETCH, "http://allowed.test/"],
        env=_proxy_env(credentials),
        timeout_s=20,
    )
    assert revoked.stdout.startswith("407")


@confined
async def test_a_preview_is_reached_through_the_mux(vm, tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    (root / "index.html").write_text("hello preview")
    await GuestWorkspace(vm, "s-web", root).bring_in()
    await vm.start_job(
        "s-web",
        "preview-s-web",
        ["python3", "-m", "http.server", "8000", "--bind", "127.0.0.1"],
        env={},
        publish=8000,
    )
    port = await vm.open_preview("s-web")
    try:
        body = b""
        for _ in range(50):
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", port.port)
                writer.write(b"GET /index.html HTTP/1.0\r\n\r\n")
                await writer.drain()
                body = await reader.read()
                writer.close()
            except OSError:
                body = b""
            if b"hello preview" in body:
                break
            await asyncio.sleep(0.2)
        assert b"hello preview" in body
        assert (await vm.job("preview-s-web")).running
    finally:
        await port.close()
        await vm.stop_job("preview-s-web")
    for _ in range(50):
        if not (await vm.job("preview-s-web")).running:
            break
        await asyncio.sleep(0.1)
    assert not (await vm.job("preview-s-web")).running


# --- what the host accepts back ---------------------------------------------------------


def _archive(tmp_path: Path, *members: tuple[tarfile.TarInfo, bytes | None]) -> Path:
    path = tmp_path / "out.tar"
    with tarfile.open(path, "w") as archive:
        for info, data in members:
            archive.addfile(info, io.BytesIO(data) if data is not None else None)
    return path


def _file(name: str, data: bytes = b"x") -> tuple[tarfile.TarInfo, bytes]:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    return info, data


def _link(name: str, target: str, kind: bytes = tarfile.SYMTYPE) -> tuple[tarfile.TarInfo, None]:
    info = tarfile.TarInfo(name)
    info.type = kind
    info.linkname = target
    return info, None


@pytest.mark.parametrize("name", ["../escape", "/etc/passwd", "a/../../escape"])
def test_a_path_outside_the_workspace_is_refused(tmp_path, name):
    with pytest.raises(VMError):
        unpack(tmp_path / "ws", _archive(tmp_path, _file(name)), [], max_bytes=1024)


def test_a_write_through_a_planted_symlink_is_refused(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    archive = _archive(tmp_path, _link("door", str(outside)), _file("door/planted"))
    with pytest.raises(VMError):
        unpack(tmp_path / "ws", archive, [], max_bytes=1024)
    assert not (outside / "planted").exists()


def test_a_symlink_is_stored_and_never_followed(tmp_path):
    root = tmp_path / "ws"
    unpack(root, _archive(tmp_path, _link("python", "/usr/bin/python3")), [], max_bytes=1024)
    assert (root / "python").is_symlink()


def test_hard_links_and_devices_are_refused(tmp_path):
    with pytest.raises(VMError):
        unpack(
            tmp_path / "ws",
            _archive(tmp_path, _link("hard", "/etc/passwd", tarfile.LNKTYPE)),
            [],
            max_bytes=1024,
        )
    device = tarfile.TarInfo("dev")
    device.type = tarfile.CHRTYPE
    with pytest.raises(VMError):
        unpack(tmp_path / "ws2", _archive(tmp_path, (device, None)), [], max_bytes=1024)


def test_more_than_the_transfer_limit_is_refused(tmp_path):
    with pytest.raises(VMError):
        unpack(tmp_path / "ws", _archive(tmp_path, _file("big", b"x" * 2048)), [], max_bytes=1024)


def test_a_deletion_never_reaches_through_a_symlink(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("mine")
    (root / "door").symlink_to(outside)
    unpack(root, _archive(tmp_path), ["door/keep"], max_bytes=1024)
    assert (outside / "keep").exists()


def test_a_listing_from_the_guest_is_parsed_defensively():
    from services.vm.sync import parse_manifest

    raw = json.loads('{"a": ["f", 1, 2], "b": "junk", "c": ["d", 0, 0, 9]}')
    assert parse_manifest(raw) == {"a": ("f", 1, 2)}
