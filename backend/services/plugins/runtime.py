"""Runs plugin tools in the platform VM.

Each plugin version is installed once per boot into ``/plugins/<name>/<digest>``: its
files under ``src`` and a virtualenv holding its requirements, built by a confined
command that can reach only the package index, then made read-only. A tool call is a
fresh process in a fresh empty workspace, with that directory read-only at ``/plugin``,
the call's arguments on stdin, and a network that reaches only the plugin's declared
hosts. A secret is a placeholder in the environment, swapped for its value by the
egress gateway in requests to the secret's host (:mod:`services.vm.intercept`).
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import secrets
import shutil
import tarfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from core.serde import parse_json
from services.revisions import Bundle, encode_bundle
from services.sandbox.limits import TIMEOUT_EXIT
from services.sandbox.session import proxy_env
from services.vm.driver import VMError
from services.vm.egress import ALLOW_FILE, Secret
from services.vm.platform import SANDBOX_UID, PlatformVM

from .manifest import PluginManifest, PluginTool
from .store import SecretValue

CALL_TIMEOUT_S = 120.0
INSTALL_TIMEOUT_S = 900.0
#: The most of a tool's result handed back; the rest is cut.
RESULT_MAX_CHARS = 64 * 1024
_LOG_TAIL_CHARS = 2000
_INSTALL_HOSTS = ("pypi.org", "files.pythonhosted.org")
_CA_PATH = "/usr/local/share/ca-certificates/hephaestus-egress.crt"

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
    def __init__(self, vm: PlatformVM, egress_root: Path) -> None:
        self._vm: PlatformVM = vm
        self._egress_root: Path = egress_root
        self._installed: dict[str, int] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._trusted_generation: int = -1

    @property
    def available(self) -> bool:
        return self._vm.available

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
        version = await self._install(manifest, files)
        declared = [name for name in sorted(manifest.secrets) if name in secret_values]
        sent = [
            Secret(
                f"hephaestus-secret-{secrets.token_hex(16)}",
                secret_values[name].value,
                secret_values[name].domain,
            )
            for name in declared
        ]
        env = {name: secret.placeholder for name, secret in zip(declared, sent, strict=True)}
        if sent:
            await self._trust()
        key = f"plugin-{secrets.token_hex(8)}"
        allow_dir = self._allow(key, [*hosts, *(secret.host for secret in sent)])
        credentials = self._vm.gateway.grant(allow_dir, sent)
        try:
            result = await self._vm.run(
                key,
                ["/plugin/venv/bin/python", "-I", "-c", _RUNNER, tool.module, tool.function],
                env={**proxy_env(credentials), **env},
                timeout_s=timeout_s,
                stdin=json.dumps(dict(args)),
                plugin=version,
            )
        finally:
            self._vm.gateway.revoke(credentials)
            shutil.rmtree(allow_dir, ignore_errors=True)
            await self._vm.wipe(key)
        log = result.stderr[-_LOG_TAIL_CHARS:]
        if result.exit_code == TIMEOUT_EXIT:
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

    async def _install(self, manifest: PluginManifest, files: Bundle) -> str:
        digest = hashlib.sha256(encode_bundle(files)).hexdigest()[:24]
        version = f"{manifest.name}/{digest}"
        lock = self._locks.setdefault(version, asyncio.Lock())
        async with lock:
            if self._installed.get(version) == self._vm.generation:
                return version
            generation = self._vm.generation
            directory = self._vm.layout.plugin(version)
            local = self._vm.scratch_path(".tar")
            try:
                await asyncio.to_thread(_pack, local, manifest, files)
                staged = await self._vm.put_path(local)
            finally:
                local.unlink(missing_ok=True)
            uid = str(SANDBOX_UID)
            _ = await self._vm.call_checked(
                [
                    "sh",
                    "-c",
                    'rm -rf "$1" && mkdir -p "$1" && tar -xf "$2" -C "$1" --no-same-owner '
                    + '&& chown -R "$3:$3" "$1"; status=$?; rm -f "$2"; exit $status',
                    "sh",
                    directory,
                    staged,
                    uid,
                ],
                what=f"unpack plugin {manifest.name}",
            )
            key = f"plugin-install-{secrets.token_hex(8)}"
            allow_dir = self._allow(key, _INSTALL_HOSTS)
            credentials = self._vm.gateway.grant(allow_dir)
            install = "python3 -m venv /plugin/venv"
            if manifest.requirements:
                install += (
                    " && /plugin/venv/bin/pip install --no-cache-dir "
                    "--disable-pip-version-check -r /plugin/requirements.txt"
                )
            try:
                result = await self._vm.run(
                    key,
                    ["sh", "-c", install],
                    env=proxy_env(credentials),
                    timeout_s=INSTALL_TIMEOUT_S,
                    plugin=version,
                    plugin_writable=True,
                )
            finally:
                self._vm.gateway.revoke(credentials)
                shutil.rmtree(allow_dir, ignore_errors=True)
                await self._vm.wipe(key)
            if result.exit_code != 0:
                tail = (result.stderr or result.stdout)[-_LOG_TAIL_CHARS:]
                raise PluginError(f"installing {manifest.name}'s requirements failed:\n{tail}")
            _ = await self._vm.call_checked(
                ["sh", "-c", 'chown -R root:root "$1" && chmod -R a+rX,go-w "$1"', "sh", directory],
                what=f"seal plugin {manifest.name}",
            )
            self._installed[version] = generation
            return version

    async def _trust(self) -> None:
        """Have the guest trust the CA that intercepted requests are served under."""
        interceptor = self._vm.gateway.interceptor
        if interceptor is None:
            raise PluginError("this installation cannot send plugin secrets")
        if self._trusted_generation == self._vm.generation:
            return
        generation = self._vm.generation
        try:
            await self._vm.put_text(_CA_PATH, interceptor.ca_pem)
            _ = await self._vm.call_checked(["update-ca-certificates"], what="trust the egress CA")
        except VMError as exc:
            raise PluginError(str(exc)) from exc
        self._trusted_generation = generation

    def _allow(self, key: str, hosts: Iterable[str]) -> Path:
        directory = self._egress_root / key
        directory.mkdir(parents=True, exist_ok=True)
        _ = (directory / ALLOW_FILE).write_text("".join(f"{host}\n" for host in sorted(set(hosts))))
        return directory


def _pack(target: Path, manifest: PluginManifest, files: Bundle) -> None:
    entries = {f"src/{path}": blob for path, blob in files.items()}
    entries["requirements.txt"] = "".join(f"{line}\n" for line in manifest.requirements).encode()
    with tarfile.open(target, "w") as archive:
        for name, blob in sorted(entries.items()):
            info = tarfile.TarInfo(name)
            info.size = len(blob)
            info.mode = 0o644
            archive.addfile(info, io.BytesIO(blob))
