"""Plugin tools run in smol machines: installed once on the host, then each call boots a
machine with the code read-only and only the declared hosts reachable."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import override

import pytest
import smol

from services.plugins import PluginError, PluginManifest, PluginRuntime, validate_files
from services.plugins.store import SecretValue
from services.vm import GuestImage, Platform

MANIFEST = """\
name: probe
description: A probe.
requirements: [httpx]
egress: [allowed.example.com]
secrets:
  API_KEY: api.example.com
tools:
  - {name: echo, description: Echo, entry: probe:echo}
"""

SECRET = SecretValue(value="s3cret", domain="api.example.com")


class FakeMachine:
    def __init__(self, config: smol.MachineConfig, handler: Callable[[list[str]], smol.ExecResult]):
        self.config: smol.MachineConfig = config
        self.mounts: list[smol.MountSpec] = list(config.mounts or [])
        self.credentials: list[smol.CredentialSpec] = list(config.credentials or [])
        resources = config.resources
        self.allow_hosts: list[str] = list(resources.allow_hosts or []) if resources else []
        self.execs: list[tuple[list[str], smol.ExecOptions | None]] = []
        self.deleted: bool = False
        self._handler: Callable[[list[str]], smol.ExecResult] = handler

    async def exec(
        self, command: list[str], opts: smol.ExecOptions | None = None
    ) -> smol.ExecResult:
        self.execs.append((command, opts))
        return self._handler(command)

    async def delete(self) -> bool:
        self.deleted = True
        return True


class FakePlatform(Platform):
    def __init__(self, tmp_path: Path) -> None:
        super().__init__(GuestImage(tmp_path / "images"))

    @property
    @override
    def available(self) -> bool:
        return True

    @override
    def require(self) -> str:
        return "/image"


class Machines:
    def __init__(self) -> None:
        self.made: list[FakeMachine] = []
        self.call_reply: Callable[[], smol.ExecResult] = lambda: _ok({"echo": "hi"})
        self.install_reply: smol.ExecResult = smol.ExecResult(0, "", "")

    def handle(self, command: list[str]) -> smol.ExecResult:
        if command[0] == "sh":
            return self.install_reply
        return self.call_reply()

    @property
    def installs(self) -> list[FakeMachine]:
        return [m for m in self.made if not m.mounts[0].read_only]

    @property
    def calls(self) -> list[FakeMachine]:
        return [m for m in self.made if m.mounts[0].read_only]


def _ok(result: object) -> smol.ExecResult:
    return smol.ExecResult(0, "noise\n" + json.dumps({"ok": True, "result": result}), "log")


@pytest.fixture
def machines(monkeypatch: pytest.MonkeyPatch) -> Machines:
    fake = Machines()

    async def create(config: smol.MachineConfig, options: smol.ConnectOptions) -> FakeMachine:
        _ = options
        machine = FakeMachine(config, fake.handle)
        fake.made.append(machine)
        return machine

    monkeypatch.setattr(smol.AsyncMachine, "create", create)
    return fake


@pytest.fixture
def runtime(tmp_path: Path, machines: Machines) -> PluginRuntime:
    _ = machines
    return PluginRuntime(FakePlatform(tmp_path), tmp_path / "installed", prefix="heph-test")


def _plugin() -> tuple[PluginManifest, dict[str, bytes]]:
    files = {"plugin.yaml": MANIFEST.encode(), "probe.py": b"def echo(text): return text\n"}
    return validate_files(files), files


async def _call(
    runtime: PluginRuntime, secrets: dict[str, SecretValue] | None = None, timeout_s: float = 60
):
    manifest, files = _plugin()
    tool = manifest.tool("echo")
    assert tool is not None
    return await runtime.call(
        manifest,
        files,
        tool,
        {"text": "hi"},
        hosts=manifest.egress,
        secret_values=secrets or {},
        timeout_s=timeout_s,
    )


async def test_a_call_returns_the_tool_value_and_the_log(
    runtime: PluginRuntime, machines: Machines
):
    result = await _call(runtime)
    assert result.ok
    assert result.output == '{"echo": "hi"}'
    assert result.log == "log"
    command, opts = machines.calls[0].execs[0]
    assert command[:3] == ["/plugin/venv/bin/python", "-I", "-c"]
    assert command[-2:] == ["probe", "echo"]
    assert opts is not None
    assert opts.stdin == '{"text": "hi"}'
    assert opts.timeout == 60
    assert machines.calls[0].deleted


async def test_a_version_installs_once_and_the_install_has_only_the_package_index(
    runtime: PluginRuntime, machines: Machines, tmp_path: Path
):
    _ = await _call(runtime)
    _ = await _call(runtime)
    assert len(machines.installs) == 1
    install = machines.installs[0]
    assert install.allow_hosts == ["pypi.org", "files.pythonhosted.org"]
    assert "pip install" in install.execs[0][0][-1]
    assert install.deleted
    version = next((tmp_path / "installed" / "probe").iterdir())
    assert (version / "src" / "probe.py").is_file()
    assert (version / "requirements.txt").read_text() == "httpx\n"


async def test_an_installed_version_survives_a_new_runtime(
    runtime: PluginRuntime, machines: Machines, tmp_path: Path
):
    _ = await _call(runtime)
    again = PluginRuntime(FakePlatform(tmp_path), tmp_path / "installed", prefix="heph-test")
    _ = await _call(again)
    assert len(machines.installs) == 1


async def test_a_call_mounts_the_code_read_only_and_reaches_declared_hosts_and_secret_hosts(
    runtime: PluginRuntime, machines: Machines
):
    _ = await _call(runtime, {"API_KEY": SECRET})
    machine = machines.calls[0]
    mount = machine.mounts[0]
    assert mount.target == "/plugin"
    assert mount.read_only
    assert machine.allow_hosts == ["allowed.example.com", "api.example.com"]
    (credential,) = machine.credentials
    assert credential.env_var == "API_KEY"
    assert credential.hosts == ["api.example.com"]
    assert credential.value == "s3cret"
    assert machine.config.persistent


async def test_no_hosts_means_no_network(runtime: PluginRuntime, machines: Machines):
    manifest, files = _plugin()
    tool = manifest.tool("echo")
    assert tool is not None
    _ = await runtime.call(manifest, files, tool, {}, hosts=[], secret_values={})
    machine = machines.calls[0]
    assert not machine.allow_hosts
    assert not machine.credentials


async def test_a_timeout_is_a_failed_result(runtime: PluginRuntime, machines: Machines):
    machines.call_reply = lambda: smol.ExecResult(124, "", "")
    result = await _call(runtime, timeout_s=5)
    assert not result.ok
    assert result.output == "echo took longer than 5s"
    assert machines.calls[0].deleted


async def test_output_that_is_not_a_result_is_an_error(runtime: PluginRuntime, machines: Machines):
    machines.call_reply = lambda: smol.ExecResult(1, "not json", "Traceback")
    result = await _call(runtime)
    assert not result.ok
    assert result.output == "echo exited with status 1"
    assert result.log == "Traceback"


async def test_a_tool_error_is_reported(runtime: PluginRuntime, machines: Machines):
    machines.call_reply = lambda: smol.ExecResult(
        0, json.dumps({"ok": False, "error": "ValueError: no luck"}), "Traceback"
    )
    result = await _call(runtime)
    assert not result.ok
    assert result.output == "ValueError: no luck"


async def test_a_failed_install_raises_and_leaves_nothing_behind(
    runtime: PluginRuntime, machines: Machines, tmp_path: Path
):
    machines.install_reply = smol.ExecResult(1, "", "no matching distribution")
    with pytest.raises(PluginError, match="no matching distribution"):
        _ = await _call(runtime)
    assert not list((tmp_path / "installed" / "probe").iterdir())
    assert machines.installs[0].deleted
    assert not machines.calls
