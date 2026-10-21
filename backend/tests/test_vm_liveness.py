"""The platform VM's liveness probe: when a missed probe means dead, and what follows."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import final, override

import pytest
from pydantic import JsonValue, TypeAdapter

from services.vm.driver import ExecResult, VMDriver
from services.vm.liveness import VMLiveness
from services.vm.platform import PlatformVM
from services.vm.project import ProjectCopies
from services.vm.workspace import LOST_TO_RESTART, GuestWorkspace
from tools.shell import shell_toolset

from ._json import arr, s
from .shell_support import Shell, checkout_ctx, scratch_sessions
from .vm_fakes import FakeVM

_RESULT = TypeAdapter(dict[str, JsonValue])


class _Driver(VMDriver):
    """A VM that boots at once; ``answers`` says whether its probe succeeds."""

    def __init__(self) -> None:
        self.phase: str | None = None
        self.step: str | None = None
        self.answers: bool = True
        self.boots: int = 0

    @override
    async def boot(self) -> None:
        self.boots += 1

    @override
    async def stop(self) -> None:
        return None

    @override
    async def alive(self) -> bool:
        return self.answers

    @override
    async def exec(self, argv: list[str], *, timeout_s: float) -> ExecResult:
        return ExecResult(0, "RELAY-READY", "")

    @override
    async def put_file(self, local: Path, guest: str) -> None:
        return None

    @override
    async def get_file(self, guest: str, local: Path, *, max_bytes: int) -> None:
        return None

    @override
    async def forward(self, guest_port: int) -> int:
        return 1


class _Clock:
    def __init__(self) -> None:
        self.now: float = 1000.0

    def __call__(self) -> float:
        return self.now


@final
class _Rig:
    def __init__(self, tmp_path: Path) -> None:
        self.driver: _Driver = _Driver()
        self.vm: PlatformVM = PlatformVM(self.driver, scratch=tmp_path)
        self.clock: _Clock = _Clock()
        self.notices: list[tuple[str, str]] = []
        self.liveness: VMLiveness = VMLiveness(self.vm, self._notify, clock=self.clock)

    async def _notify(self, title: str, body: str) -> None:
        self.notices.append((title, body))

    async def up(self) -> None:
        await self.vm.start()
        assert await self.vm.wait_ready(5.0)

    async def die_and_probe_twice(self) -> None:
        self.driver.answers = False
        await self.liveness.probe()
        await self.liveness.probe()

    async def settle(self) -> None:
        assert await self.vm.wait_ready(5.0)


async def test_one_missed_probe_does_not_reset(tmp_path: Path):
    rig = _Rig(tmp_path)
    await rig.up()
    rig.driver.answers = False
    await rig.liveness.probe()
    rig.driver.answers = True
    await rig.liveness.probe()
    rig.driver.answers = False
    await rig.liveness.probe()
    assert rig.driver.boots == 1 and rig.vm.available and not rig.notices
    await rig.vm.stop()


async def test_two_missed_probes_in_a_row_reset_and_notify(tmp_path: Path):
    rig = _Rig(tmp_path)
    await rig.up()
    await rig.die_and_probe_twice()

    assert len(rig.notices) == 1 and "restarted" in rig.notices[0][1]
    rig.driver.answers = True
    await rig.settle()
    assert rig.driver.boots == 2
    await rig.vm.stop()


async def test_the_vm_reads_unavailable_the_moment_it_is_judged_dead(tmp_path: Path):
    rig = _Rig(tmp_path)
    await rig.up()
    rig.vm.mark_unavailable("the platform VM stopped answering")
    assert not rig.vm.available
    assert rig.vm.failure == "the platform VM stopped answering"
    await rig.liveness.probe()
    assert rig.driver.boots == 1
    await rig.vm.stop()


async def test_a_manual_reset_of_a_held_vm_earns_the_automatic_reset_again(tmp_path: Path):
    rig = _Rig(tmp_path)
    await rig.up()
    await rig.die_and_probe_twice()
    rig.driver.answers = True
    await rig.settle()
    rig.clock.now += 60
    await rig.die_and_probe_twice()
    assert not rig.vm.available

    await rig.vm.reset()
    rig.driver.answers = True
    await rig.settle()
    await rig.liveness.probe()
    rig.clock.now += 60
    boots = rig.driver.boots
    await rig.die_and_probe_twice()
    rig.driver.answers = True
    await rig.settle()

    assert rig.driver.boots == boots + 1


async def test_a_second_death_within_ten_minutes_stays_down(tmp_path: Path):
    rig = _Rig(tmp_path)
    await rig.up()
    await rig.die_and_probe_twice()
    rig.driver.answers = True
    await rig.settle()

    rig.clock.now += 300
    await rig.die_and_probe_twice()

    assert rig.driver.boots == 2
    assert not rig.vm.available
    assert "left down" in rig.notices[-1][1]
    await rig.liveness.probe()
    await asyncio.sleep(0)
    assert rig.driver.boots == 2
    await rig.vm.stop()


async def test_a_death_after_the_window_is_reset_again(tmp_path: Path):
    rig = _Rig(tmp_path)
    await rig.up()
    await rig.die_and_probe_twice()
    rig.driver.answers = True
    await rig.settle()

    rig.clock.now += 601
    await rig.die_and_probe_twice()
    rig.driver.answers = True
    await rig.settle()

    assert rig.driver.boots == 3
    await rig.vm.stop()


async def test_recovery_after_a_reset_resumes_probing(tmp_path: Path):
    rig = _Rig(tmp_path)
    await rig.up()
    await rig.die_and_probe_twice()
    rig.driver.answers = True
    await rig.settle()
    await rig.liveness.probe()
    assert rig.vm.available and len(rig.notices) == 1
    await rig.vm.stop()


async def test_a_vm_that_is_still_booting_is_not_probed_dead(tmp_path: Path):
    rig = _Rig(tmp_path)
    rig.driver.answers = False
    await rig.liveness.probe()
    await rig.liveness.probe()
    assert rig.driver.boots == 0 and not rig.notices


async def test_a_bring_out_from_a_stale_generation_returns_the_note(tmp_path: Path):
    vm = FakeVM(tmp_path / "guest")
    root = tmp_path / "ws"
    root.mkdir()
    workspace = GuestWorkspace(vm, "k", root)
    await workspace.bring_in()
    assert await workspace.bring_out() is None

    vm.restart()

    assert await workspace.bring_out() == LOST_TO_RESTART


async def test_a_bring_out_from_a_workspace_never_brought_in_says_nothing(tmp_path: Path):
    workspace = GuestWorkspace(FakeVM(tmp_path / "guest"), "k", tmp_path)
    assert await workspace.bring_out() is None


async def test_the_shell_result_carries_the_restart_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    repo = tmp_path / "project"
    repo.mkdir()
    vm = FakeVM(tmp_path / "guest")
    vm.alive_after_start = False
    read_guest = vm.read_guest

    async def rebuilt_after_the_command(guest: str, *, max_bytes: int) -> str:
        vm.restart()
        return await read_guest(guest, max_bytes=max_bytes)

    monkeypatch.setattr(vm, "read_guest", rebuilt_after_the_command)
    copies = ProjectCopies(vm, idle_ttl_s=3600, reap_interval_s=3600)
    sessions = await scratch_sessions(tmp_path, vm)
    ctx = checkout_ctx(repo, copies=copies, sessions=sessions)
    shell = Shell(shell_toolset(), ctx)

    result = _RESULT.validate_python(await shell.call("run_command", command="echo hi"))

    assert LOST_TO_RESTART in [s(note) for note in arr(result["notes"])]
