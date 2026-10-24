"""Runs plugin tools in smol machines.

Each plugin version is installed once into ``<root>/<name>/<digest>`` on the host: its files
under ``src`` and a virtualenv holding its requirements, built by a machine that can reach
only the package index. A tool call boots a fresh machine with that directory read-only at
``/plugin``, the call's arguments on stdin, and a network that reaches only the plugin's
declared hosts. A secret is a placeholder in the guest environment, swapped for its value by
smol in HTTPS requests to the secret's host.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import shutil
from collections.abc import Iterable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

import smol

from core.serde import parse_json
from services.revisions import Bundle, encode_bundle
from services.vm.platform import Platform
from services.vm.target import LOCAL

from .manifest import PluginManifest, PluginTool
from .store import SecretValue

CALL_TIMEOUT_S = 120.0
INSTALL_TIMEOUT_S = 900.0
#: A memory bound on one result, which a script's call receives whole. The context is
#: protected by spilling (`tools/spill.py`), not by this.
RESULT_MAX_CHARS = 2_000_000
_LOG_TAIL_CHARS = 2000
_INSTALL_HOSTS = ("pypi.org", "files.pythonhosted.org")
_TIMEOUT_EXIT = 124
_MARKER = ".installed"

# Runs inside the plugin's venv with -I, so only the venv and /plugin/src are importable.
# Anything the tool prints goes to stderr; stdout carries the one result line.
_RUNNER = """
import asyncio, importlib, inspect, json, os, sys
sys.path.insert(0, "/plugin/src")
out = os.fdopen(os.dup(1), "w")
os.dup2(2, 1)
try:
    args = json.load(sys.stdin)
    result = getattr(importlib.import_module(sys.argv[1]), sys.argv[2])(**args)
    if inspect.isawaitable(result):
        result = asyncio.run(result)
    out.write(json.dumps({"ok": True, "result": result}, default=str))
except BaseException as exc:
    import traceback
    traceback.print_exc()
    out.write(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}))
out.flush()
"""


class PluginError(Exception):
    """A plugin could not be installed or run; the message is for the agent."""


@dataclass(frozen=True)
class CallResult:
    ok: bool
    #: The tool's return value as JSON text, or the error.
    output: str
    #: The end of what the tool printed.
    log: str


class PluginRuntime:
    def __init__(self, platform: Platform, root: Path, *, prefix: str) -> None:
        self._platform: Platform = platform
        self._root: Path = root
        self._prefix: str = prefix
        self._locks: dict[str, asyncio.Lock] = {}

    @property
    def available(self) -> bool:
        return self._platform.available

    async def call(
        self,
        manifest: PluginManifest,
        files: Bundle,
        tool: PluginTool,
        args: Mapping[str, object],
        *,
        hosts: Iterable[str],
        secret_values: Mapping[str, SecretValue],
        timeout_s: float = CALL_TIMEOUT_S,
    ) -> CallResult:
        """Run ``tool`` with ``args``. ``hosts`` are what it may reach; each secret also
        reaches its own host."""
        directory = await self._install(manifest, files)
        declared = [name for name in sorted(manifest.secrets) if name in secret_values]
        credentials = [
            smol.CredentialSpec(
                name=f"s{index}",
                env_var=name,
                hosts=[secret_values[name].domain],
                value=secret_values[name].value,
            )
            for index, name in enumerate(declared)
        ]
        reach = sorted({*hosts, *(secret_values[name].domain for name in declared)})
        config = self._config(
            "plugin",
            directory,
            read_only=True,
            allow_hosts=reach,
            credentials=credentials,
        )
        argv = ["/plugin/venv/bin/python", "-I", "-c", _RUNNER, tool.module, tool.function]
        result = await self._run(config, argv, timeout_s=timeout_s, stdin=json.dumps(dict(args)))
        log = result.stderr[-_LOG_TAIL_CHARS:]
        if result.exit_code == _TIMEOUT_EXIT:
            return CallResult(False, f"{tool.name} took longer than {timeout_s:.0f}s", log)
        try:
            reply = parse_json(result.stdout.strip().splitlines()[-1])
        except IndexError, ValueError:
            return CallResult(False, f"{tool.name} exited with status {result.exit_code}", log)
        if not isinstance(reply, dict):
            return CallResult(False, f"{tool.name} exited with status {result.exit_code}", log)
        if not reply.get("ok"):
            return CallResult(False, str(reply.get("error", "the tool failed")), log)
        output = json.dumps(reply.get("result"), ensure_ascii=False)
        if len(output) > RESULT_MAX_CHARS:
            output = output[:RESULT_MAX_CHARS] + " … [cut: the result was too long]"
        return CallResult(True, output, log)

    async def _install(self, manifest: PluginManifest, files: Bundle) -> Path:
        digest = hashlib.sha256(encode_bundle(files)).hexdigest()[:24]
        directory = self._root / manifest.name / digest
        async with self._locks.setdefault(f"{manifest.name}/{digest}", asyncio.Lock()):
            if (directory / _MARKER).is_file():
                return directory
            try:
                await asyncio.to_thread(_stage, directory, manifest, files)
                install = "python3 -m venv /plugin/venv"
                if manifest.requirements:
                    install += (
                        " && /plugin/venv/bin/pip install --no-cache-dir "
                        "--disable-pip-version-check -r /plugin/requirements.txt"
                    )
                config = self._config(
                    "plugin-install",
                    directory,
                    read_only=False,
                    allow_hosts=_INSTALL_HOSTS,
                    credentials=[],
                )
                result = await self._run(config, ["sh", "-c", install], timeout_s=INSTALL_TIMEOUT_S)
                if result.exit_code != 0:
                    tail = (result.stderr or result.stdout)[-_LOG_TAIL_CHARS:]
                    raise PluginError(f"installing {manifest.name}'s requirements failed:\n{tail}")
                _ = (directory / _MARKER).write_text(digest)
            except BaseException:
                shutil.rmtree(directory, ignore_errors=True)
                raise
            return directory

    def _config(
        self,
        kind: str,
        directory: Path,
        *,
        read_only: bool,
        allow_hosts: Iterable[str],
        credentials: list[smol.CredentialSpec],
    ) -> smol.MachineConfig:
        hosts = list(allow_hosts)
        resources = smol.ResourceSpec()
        if hosts:
            resources.allow_hosts = hosts
        return smol.MachineConfig(
            name=f"{self._prefix}-{kind}-{secrets.token_hex(6)}",
            image=self._platform.require(),
            mounts=[smol.MountSpec(source=str(directory), target="/plugin", read_only=read_only)],
            resources=resources,
            credentials=credentials,
            persistent=True,
        )

    async def _run(
        self,
        config: smol.MachineConfig,
        argv: list[str],
        *,
        timeout_s: float,
        stdin: str | None = None,
    ) -> smol.ExecResult:
        try:
            machine = await smol.AsyncMachine.create(config, LOCAL)
        except smol.SmolError as exc:
            raise PluginError(f"the VM did not start: {exc}") from exc
        try:
            return await machine.exec(argv, smol.ExecOptions(timeout=timeout_s, stdin=stdin))
        except smol.SmolError as exc:
            raise PluginError(f"the VM could not run the command: {exc}") from exc
        finally:
            with suppress(smol.SmolError):
                _ = await machine.delete()


def _stage(directory: Path, manifest: PluginManifest, files: Bundle) -> None:
    root = directory.resolve()
    shutil.rmtree(root, ignore_errors=True)
    entries = {f"src/{path}": blob for path, blob in files.items()}
    entries["requirements.txt"] = "".join(f"{line}\n" for line in manifest.requirements).encode()
    for name, blob in entries.items():
        target = (root / name).resolve()
        if not target.is_relative_to(root):
            raise PluginError(f"{name} is outside the plugin's directory")
        target.parent.mkdir(parents=True, exist_ok=True)
        _ = target.write_bytes(blob)
