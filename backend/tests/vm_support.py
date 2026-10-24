"""A stand-in for the smol SDK, so no test boots a machine."""

from __future__ import annotations

from pathlib import Path
from typing import final, override

import pytest
import smol

from services.vm.image import GuestImage
from services.vm.platform import Platform


@final
class FakeMachine(smol.AsyncMachine):
    """A machine that records what it is asked and answers every command with success."""

    def __init__(self, config: smol.MachineConfig) -> None:
        super().__init__(smol.Machine.__new__(smol.Machine))
        self.config: smol.MachineConfig = config
        self.deleted: bool = False
        self.commands: list[list[str]] = []
        self.exit_code: int = 0
        self.written: dict[str, str | bytes] = {}
        self.published: list[int] = []

    @property
    @override
    def id(self) -> str:
        return self.config.name or "unnamed"

    @property
    @override
    def name(self) -> str:
        return self.config.name or "unnamed"

    @override
    async def exec(
        self, command: list[str], opts: smol.ExecOptions | None = None
    ) -> smol.ExecResult:
        self.commands.append(command)
        return smol.ExecResult(exit_code=self.exit_code, stdout="", stderr="")

    @override
    async def write_file(self, path: str, data: bytes | str, mode: int | None = None) -> None:
        self.written[path] = data

    @override
    async def publish(
        self, guest: int, host: int | None = None, protocol: str = "tcp"
    ) -> smol.PublishedPort:
        self.published.append(guest)
        return smol.PublishedPort(guest=guest, host=40000 + guest)

    @override
    async def delete(
        self, include_usage: bool = False, drain_secs: float | None = None
    ) -> smol.MachineUsageReport | None:
        self.deleted = True
        return None


@final
class FakeSmol:
    """Patches the SDK's entry points; ``machines`` is every machine created, in order."""

    def __init__(self) -> None:
        self.machines: list[FakeMachine] = []
        self.deleted_names: list[str] = []
        self.create_error: smol.SmolError | None = None

    async def create(
        self, config: smol.MachineConfig | None = None, _conn: smol.ConnectOptions | None = None
    ) -> FakeMachine:
        if self.create_error is not None:
            raise self.create_error
        assert config is not None
        machine = FakeMachine(config)
        self.machines.append(machine)
        return machine

    async def delete_by_name(
        self,
        machine_id: str,
        _conn: smol.ConnectOptions | None = None,
        _drain_secs: float | None = None,
    ) -> None:
        self.deleted_names.append(machine_id)

    @property
    def live(self) -> list[FakeMachine]:
        return [m for m in self.machines if not m.deleted]


def install_fake_smol(monkeypatch: pytest.MonkeyPatch) -> FakeSmol:
    fake = FakeSmol()
    monkeypatch.setattr(smol.AsyncMachine, "create", fake.create)
    monkeypatch.setattr(smol.AsyncMachine, "delete_by_name", fake.delete_by_name)
    monkeypatch.setattr(smol, "local_availability", lambda: (True, "", ""))
    return fake


async def ready_platform(tmp_path: Path) -> Platform:
    """A platform whose image exists, brought up through the real bring-up path."""
    image = tmp_path / "image.tar"
    _ = image.write_bytes(b"")
    platform = Platform(GuestImage(tmp_path / "images", override=image))
    await platform.start()
    assert await platform.wait_ready(5.0)
    return platform
