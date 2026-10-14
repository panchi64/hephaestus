"""What the smolvm driver sends the guest, run the way a PATH-less guest agent runs it."""

from __future__ import annotations

import itertools
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import pytest

from services.vm import VMError
from services.vm import smolvm as smolvm_module
from services.vm.smolvm import SmolvmDriver, provision


@dataclass
class _Ran:
    exit_code: int
    stdout: str
    stderr: str


class _AgentWithoutPath:
    """Runs a command the way smolvm's vsock agent does: from an init with no PATH."""

    def run(self, command: str, *, timeout: int, shell: str) -> _Ran:
        done = subprocess.run(
            ["/bin/sh", "-c", command], env={}, capture_output=True, text=True, check=False
        )
        return _Ran(done.returncode, done.stdout, done.stderr)


async def test_a_command_finds_its_tools_without_the_agents_path(tmp_path, monkeypatch):
    driver = SmolvmDriver(
        state_dir=tmp_path,
        cpus=1,
        memory_mb=512,
        disk_gb=1,
        boot_timeout_s=1,
        provision_timeout_s=1,
    )
    monkeypatch.setattr(SmolvmDriver, "_require", lambda self: _AgentWithoutPath())

    result = await driver.exec(
        ["python3", "-c", "import os, sys; print(sys.executable); print(os.environ['HOME'])"],
        timeout_s=30,
    )

    assert result.exit_code == 0, result.stderr
    executable, home = result.stdout.splitlines()
    assert Path(executable).is_absolute()
    assert home == "/root"


class _BuildingGuest:
    """A provisioning guest whose log advances one scripted probe at a time."""

    def __init__(self, probes: Iterable[str], tail: str = "") -> None:
        self._probes = iter(probes)
        self._tail = tail
        self.commands: list[str] = []

    def run(self, command: str, *, timeout: int, shell: str) -> _Ran:
        self.commands.append(command)
        if "provision.exit 2>/dev/null" in command:
            return _Ran(0, next(self._probes), "")
        if command.endswith("/root/provision.log") and " tail " in f" {command} ":
            return _Ran(0, self._tail, "")
        return _Ran(0, "", "")


def test_an_image_build_reports_each_step_it_reaches(monkeypatch):
    monkeypatch.setattr(smolvm_module, "_PROVISION_POLL_S", 0)
    guest = _BuildingGuest(["\n+ apt-get update\n", "\n+ apt-get update\n", "0\n+ sync\n"])
    steps: list[str] = []

    provision(guest, ["bash", "/root/provision.sh"], timeout_s=60, on_step=steps.append)

    assert "setsid" in guest.commands[0]
    assert steps == ["apt-get update", "sync"]


def test_a_failed_build_reports_the_end_of_its_log(monkeypatch):
    monkeypatch.setattr(smolvm_module, "_PROVISION_POLL_S", 0)
    guest = _BuildingGuest(["1\n+ pip install\n"], tail="ModuleNotFoundError: typing_extensions")

    with pytest.raises(VMError, match="typing_extensions"):
        provision(guest, ["bash", "/root/provision.sh"], timeout_s=60, on_step=lambda _: None)


def test_a_build_that_never_finishes_times_out(monkeypatch):
    monkeypatch.setattr(smolvm_module, "_PROVISION_POLL_S", 0)
    guest = _BuildingGuest(itertools.repeat("\n"))

    with pytest.raises(VMError, match="timed out"):
        provision(guest, ["bash", "/root/provision.sh"], timeout_s=0.05, on_step=lambda _: None)
