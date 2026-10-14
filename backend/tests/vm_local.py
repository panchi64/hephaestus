"""A :class:`~services.vm.driver.VMDriver` whose "guest" is this machine.

Commands run here as the test's own user, against a guest layout rooted in a temp dir,
so the real guest scripts (``vm/guest``) are exercised without booting a VM. The
confinement needs root and bubblewrap; tests that use it skip without them.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import socket
import tempfile
from pathlib import Path
from typing import cast

from services.vm.driver import ExecResult, VMError
from services.vm.platform import GuestLayout, PlatformVM


def can_confine() -> bool:
    return os.geteuid() == 0 and shutil.which("bwrap") is not None


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return cast(tuple[str, int], sock.getsockname())[1]


def local_layout() -> GuestLayout:
    # Short: the egress socket's path has to fit a unix socket address.
    root = tempfile.mkdtemp(prefix="ovm", dir="/tmp")
    os.chmod(root, 0o755)
    return GuestLayout(
        root=root, pool_port=_free_port(), mux_port=_free_port(), proxy_port=_free_port()
    )


class LocalDriver:
    def __init__(self) -> None:
        self.phase: str | None = None
        self.step: str | None = None
        self.running: bool = False
        self.boots: int = 0
        self._sessions: set[int] = set()

    async def boot(self) -> None:
        self.running = True
        self.boots += 1

    async def stop(self) -> None:
        self.running = False
        for sid in self._sessions:
            try:
                os.killpg(sid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        self._sessions.clear()

    async def alive(self) -> bool:
        return self.running

    async def exec(self, argv: list[str], *, timeout_s: float) -> ExecResult:
        if not self.running:
            raise VMError("not running")
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        self._sessions.add(proc.pid)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout_s)
        except TimeoutError as exc:
            proc.kill()
            raise VMError("timed out") from exc
        return ExecResult(proc.returncode or 0, out.decode(), err.decode())

    async def put_file(self, local: Path, guest: str) -> None:
        Path(guest).parent.mkdir(parents=True, exist_ok=True)
        _ = shutil.copyfile(local, guest)

    async def get_file(self, guest: str, local: Path, *, max_bytes: int) -> None:
        if Path(guest).stat().st_size > max_bytes:
            raise VMError("too large")
        _ = shutil.copyfile(guest, local)

    async def forward(self, guest_port: int) -> int:
        return guest_port


async def local_vm(tmp_path: Path) -> tuple[PlatformVM, LocalDriver]:
    driver = LocalDriver()
    vm = PlatformVM(driver, scratch=tmp_path / "xfer", layout=local_layout())
    await vm.start()
    assert await vm.wait_ready(10), vm.failure
    return vm, driver
