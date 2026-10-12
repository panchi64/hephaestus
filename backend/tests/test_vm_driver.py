"""What the smolvm driver sends the guest, run the way a PATH-less guest agent runs it."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from services.vm.smolvm import SmolvmDriver


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
