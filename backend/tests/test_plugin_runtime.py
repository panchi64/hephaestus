"""Plugin tools run with the real guest scripts on this machine: installed once, then
each call confined, read-only to its own code, and fenced to its declared hosts."""

from __future__ import annotations

import pytest

from services.plugins import PluginRuntime, validate_files

from .vm_local import can_confine, local_vm

pytestmark = pytest.mark.skipif(not can_confine(), reason="needs root and bubblewrap")

MANIFEST = """\
name: probe
description: Probes its own confinement.
egress: [allowed.example.com]
tools:
  - {name: echo, description: Echo, entry: probe:echo}
  - {name: scribble, description: Write to own code, entry: probe:scribble}
  - {name: fetch, description: Fetch a URL, entry: probe:fetch}
  - {name: boom, description: Fail, entry: probe:boom}
"""

CODE = """
import urllib.request, urllib.error

async def echo(text):
    print("noise on stdout")
    return {"echo": text}

def scribble():
    try:
        open("/plugin/src/probe.py", "a").write("# changed")
    except OSError as exc:
        return type(exc).__name__
    return "wrote"

def fetch(url):
    try:
        with urllib.request.urlopen(url, timeout=10) as reply:
            return reply.read().decode()
    except urllib.error.HTTPError as exc:
        return f"{exc.code} {exc.read().decode()}"

def boom():
    raise ValueError("no luck")
"""


@pytest.fixture
async def runtime(tmp_path):
    platform, _driver = await local_vm(tmp_path)
    yield PluginRuntime(platform, tmp_path / "egress")
    await platform.stop()


def _plugin():
    files = {"plugin.yaml": MANIFEST.encode(), "probe.py": CODE.encode()}
    return validate_files(files), files


async def _call(runtime: PluginRuntime, tool: str, **args):
    manifest, files = _plugin()
    return await runtime.call(
        manifest,
        files,
        manifest.tool(tool),
        args,
        hosts=manifest.egress,
        secret_values={},
        timeout_s=60,
    )


async def test_a_tool_returns_its_value_and_its_prints_go_to_the_log(runtime):
    result = await _call(runtime, "echo", text="hi")
    assert result.ok, result.log
    assert result.output == '{"echo": "hi"}'
    assert "noise on stdout" in result.log


async def test_a_tool_cannot_change_its_own_code(runtime):
    result = await _call(runtime, "scribble")
    assert result.ok, result.log
    assert result.output in ('"PermissionError"', '"OSError"')


async def test_a_tool_reaches_only_its_declared_hosts(runtime):
    result = await _call(runtime, "fetch", url="http://undeclared.example.org/")
    assert result.ok, result.log
    assert "odysseus-egress: denied undeclared.example.org" in result.output


async def test_a_failing_tool_reports_its_error_and_traceback(runtime):
    result = await _call(runtime, "boom")
    assert not result.ok
    assert result.output == "ValueError: no luck"
    assert "Traceback" in result.log


async def test_a_version_installs_once_per_boot(runtime, monkeypatch):
    await _call(runtime, "echo", text="one")
    platform = runtime._vm
    installs = []
    original = platform.run

    async def counting(key, argv, **kwargs):
        if kwargs.get("plugin_writable"):
            installs.append(key)
        return await original(key, argv, **kwargs)

    monkeypatch.setattr(platform, "run", counting)
    await _call(runtime, "echo", text="two")
    assert installs == []
