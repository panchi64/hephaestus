"""``PlatformVM.wait_ready`` ends as soon as a bring-up does, success or failure."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import override

from services.vm.driver import ExecResult, VMDriver, VMError
from services.vm.platform import PlatformVM


class _BootDriver(VMDriver):
    """A driver whose boot fails at once, or hangs until cancelled."""

    def __init__(self, *, fail: bool) -> None:
        self.phase: str | None = None
        self.step: str | None = None
        self._fail: bool = fail

    @override
    async def boot(self) -> None:
        if self._fail:
            raise VMError("QEMU isn't installed on this machine.")
        _ = await asyncio.Event().wait()

    @override
    async def stop(self) -> None:
        return None

    @override
    async def alive(self) -> bool:
        return False

    @override
    async def exec(self, argv: list[str], *, timeout_s: float) -> ExecResult:
        raise VMError("not booted")

    @override
    async def put_file(self, local: Path, guest: str) -> None:
        raise VMError("not booted")

    @override
    async def get_file(self, guest: str, local: Path, *, max_bytes: int) -> None:
        raise VMError("not booted")

    @override
    async def forward(self, guest_port: int) -> int:
        raise VMError("not booted")


async def test_a_failed_bring_up_ends_the_wait_with_its_reason(tmp_path: Path) -> None:
    vm = PlatformVM(_BootDriver(fail=True), scratch=tmp_path)
    await vm.start()
    started = time.monotonic()
    assert not await vm.wait_ready(30.0)
    assert time.monotonic() - started < 5.0
    assert vm.failure == "QEMU isn't installed on this machine."
    await vm.stop()


async def test_a_bring_up_still_running_waits_only_for_the_timeout(tmp_path: Path) -> None:
    vm = PlatformVM(_BootDriver(fail=False), scratch=tmp_path)
    await vm.start()
    assert not await vm.wait_ready(0.05)
    await vm.stop()


async def test_a_vm_that_was_never_started_does_not_wait(tmp_path: Path) -> None:
    vm = PlatformVM(_BootDriver(fail=False), scratch=tmp_path)
    started = time.monotonic()
    assert not await vm.wait_ready(30.0)
    assert time.monotonic() - started < 1.0
