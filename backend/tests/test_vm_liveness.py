"""The services machine's liveness probe: when a missed probe means dead, and what follows."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import final

import pytest

from services.vm import ServicesMachine
from services.vm.liveness import VMLiveness

from .vm_support import FakeMachine, FakeSmol, install_fake_smol, ready_platform


class _Clock:
    def __init__(self) -> None:
        self.now: float = 1000.0

    def __call__(self) -> float:
        return self.now


@final
class _Rig:
    def __init__(self, fake: FakeSmol, vm: ServicesMachine) -> None:
        self.fake: FakeSmol = fake
        self.vm: ServicesMachine = vm
        self.clock: _Clock = _Clock()
        self.notices: list[tuple[str, str]] = []
        self.liveness: VMLiveness = VMLiveness(vm, self._notify, clock=self.clock)

    async def _notify(self, title: str, body: str) -> None:
        self.notices.append((title, body))

    @property
    def boots(self) -> int:
        return len(self.fake.machines)

    @property
    def machine(self) -> FakeMachine:
        return self.fake.live[-1]

    async def up(self) -> None:
        await self.vm.start()
        assert await self.vm.wait_ready(5.0)

    def stop_answering(self) -> None:
        self.machine.exit_code = 1

    async def die_and_probe_twice(self) -> None:
        self.stop_answering()
        await self.liveness.probe()
        await self.liveness.probe()

    async def settle(self) -> None:
        assert await self.vm.wait_ready(5.0)


async def _rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Rig:
    fake = install_fake_smol(monkeypatch)
    platform = await ready_platform(tmp_path)
    vm = ServicesMachine(platform, name="heph-services", cpus=1, memory_mb=256)
    return _Rig(fake, vm)


async def test_one_missed_probe_does_not_reset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rig = await _rig(tmp_path, monkeypatch)
    await rig.up()
    rig.stop_answering()
    await rig.liveness.probe()
    rig.machine.exit_code = 0
    await rig.liveness.probe()
    rig.stop_answering()
    await rig.liveness.probe()
    assert rig.boots == 1 and rig.vm.available and not rig.notices
    await rig.vm.stop()


async def test_two_missed_probes_in_a_row_reset_and_notify(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    rig = await _rig(tmp_path, monkeypatch)
    await rig.up()
    await rig.die_and_probe_twice()

    assert len(rig.notices) == 1 and "restarted" in rig.notices[0][1]
    await rig.settle()
    assert rig.boots == 2
    await rig.vm.stop()


async def test_the_vm_reads_unavailable_the_moment_it_is_judged_dead(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    rig = await _rig(tmp_path, monkeypatch)
    await rig.up()
    rig.vm.mark_unavailable("the services VM stopped answering")
    assert not rig.vm.available
    assert rig.vm.failure == "the services VM stopped answering"
    await rig.liveness.probe()
    assert rig.boots == 1
    await rig.vm.stop()


async def test_a_manual_reset_of_a_held_vm_earns_the_automatic_reset_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    rig = await _rig(tmp_path, monkeypatch)
    await rig.up()
    await rig.die_and_probe_twice()
    await rig.settle()
    rig.clock.now += 60
    await rig.die_and_probe_twice()
    assert not rig.vm.available

    await rig.vm.reset()
    await rig.settle()
    await rig.liveness.probe()
    rig.clock.now += 60
    boots = rig.boots
    await rig.die_and_probe_twice()
    await rig.settle()

    assert rig.boots == boots + 1
    await rig.vm.stop()


async def test_a_second_death_within_ten_minutes_stays_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    rig = await _rig(tmp_path, monkeypatch)
    await rig.up()
    await rig.die_and_probe_twice()
    await rig.settle()

    rig.clock.now += 300
    await rig.die_and_probe_twice()

    assert rig.boots == 2
    assert not rig.vm.available
    assert "left down" in rig.notices[-1][1]
    await rig.liveness.probe()
    await asyncio.sleep(0)
    assert rig.boots == 2
    await rig.vm.stop()


async def test_a_death_after_the_window_is_reset_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    rig = await _rig(tmp_path, monkeypatch)
    await rig.up()
    await rig.die_and_probe_twice()
    await rig.settle()

    rig.clock.now += 601
    await rig.die_and_probe_twice()
    await rig.settle()

    assert rig.boots == 3
    await rig.vm.stop()


async def test_recovery_after_a_reset_resumes_probing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    rig = await _rig(tmp_path, monkeypatch)
    await rig.up()
    await rig.die_and_probe_twice()
    await rig.settle()
    await rig.liveness.probe()
    assert rig.vm.available and len(rig.notices) == 1
    await rig.vm.stop()


async def test_a_vm_that_is_still_booting_is_not_probed_dead(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    rig = await _rig(tmp_path, monkeypatch)
    await rig.liveness.probe()
    await rig.liveness.probe()
    assert rig.boots == 0 and not rig.notices


async def test_a_reset_deletes_the_dead_machine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rig = await _rig(tmp_path, monkeypatch)
    await rig.up()
    dead = rig.machine
    await rig.die_and_probe_twice()
    await rig.settle()

    assert dead.deleted
    assert rig.machine is not dead
    await rig.vm.stop()
