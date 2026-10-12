"""The platform VM: one long-lived microVM per installation, and everything run in it.

Workspace commands, previews, headless Chromium and SearXNG all run here instead of on
the host. The VM is treated as compromisable by any decision a model makes, so what it
can reach is kept to what the current work was given:

* **Nothing is mounted.** The host pipes each active workspace in and out
  (:mod:`services.vm.workspace`) and wipes the guest copy when the workspace goes idle.
  The data dir, the vault, uploads, project checkouts and ``$HOME`` never enter it.
* **The host drives every call.** It runs commands, copies files and opens connections;
  the guest has no channel back except the ones the host holds open for it.
* **Egress goes through the host's gateway** (:mod:`services.vm.egress`): a workspace
  reaches only its allowlist, platform services only public addresses, and nothing
  reaches the host's own services or the LAN.

The guest scripts in ``backend/vm/guest`` are copied in on every boot, so they version
with the app rather than with the image. Each boot starts from the clean image, so a
reset loses nothing durable.

Bring-up runs in the background and never blocks app start; until it finishes (or if it
fails) :attr:`available` is False and the capabilities that need the VM say so.
"""

from __future__ import annotations

import asyncio
import json
import logging
import posixpath
import secrets
import shlex
import shutil
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from functools import partial
from pathlib import Path

from core.tasks import STOP_TIMEOUT_S, cancel_and_wait

from .driver import ExecResult, VMDriver, VMError
from .egress import EgressGateway
from .sync import Changes, Manifest, pack, parse_manifest, unpack
from .tunnel import EgressTunnel, PreviewPort

logger = logging.getLogger(__name__)

GUEST_SCRIPTS = Path(__file__).resolve().parents[2] / "vm" / "guest"
#: The proxy address workspace commands see, on the loopback ``enter.py`` sets up.
WORKSPACE_PROXY = "127.0.0.1:3128"
SANDBOX_UID = 1100


@dataclass(frozen=True)
class GuestLayout:
    """Where things live in the guest. Only tests move them."""

    root: str = "/"
    pool_port: int = 7101
    mux_port: int = 7102
    proxy_port: int = 3128

    def path(self, *parts: str) -> str:
        return posixpath.join(self.root, *parts)

    @property
    def opt(self) -> str:
        return self.path("opt/odysseus")

    @property
    def run_dir(self) -> str:
        return self.path("run/odysseus")

    @property
    def xfer(self) -> str:
        return self.path("run/odysseus/xfer")

    def work(self, key: str) -> str:
        return self.path("work", key)

    @property
    def platform_proxy(self) -> str:
        """Where platform services in the guest's own namespace reach the relay."""
        return f"127.0.0.1:{self.proxy_port}"


#: How much longer the host waits on a command than the guest's own timeout, which is
#: what actually ends it.
_EXEC_GRACE_S = 15.0
_ADMIN_TIMEOUT_S = 120.0
#: Guest calls in flight at once, across every caller.
_CONCURRENCY = 8


@dataclass(frozen=True)
class JobState:
    running: bool
    exit_code: int | None
    stdout: str
    stderr: str
    #: Byte offsets to pass back to read only what follows.
    out_end: int
    err_end: int


@dataclass(frozen=True)
class WorkspaceLimits:
    """What one workspace's commands may use between them; empty means uncapped."""

    memory: str = ""
    cpus: str = ""
    pids: int = 0

    def flags(self) -> list[str]:
        flags: list[str] = []
        if self.memory:
            flags += ["--memory", self.memory]
        if self.cpus:
            flags += ["--cpus", self.cpus]
        if self.pids:
            flags += ["--pids", str(self.pids)]
        return flags


class PlatformVM:
    def __init__(
        self,
        driver: VMDriver,
        *,
        scratch: Path,
        layout: GuestLayout | None = None,
        limits: WorkspaceLimits | None = None,
        enabled: bool = True,
        transfer_max_bytes: int = 4 * 1024**3,
    ) -> None:
        self._driver = driver
        self._scratch = scratch
        self.layout = layout or GuestLayout()
        self.limits = limits or WorkspaceLimits()
        self._enabled = enabled
        self._transfer_max_bytes = transfer_max_bytes
        self.gateway = EgressGateway()
        self._tunnel: EgressTunnel | None = None
        self._pool_port: int | None = None
        self._mux_port: int | None = None
        self._ready = asyncio.Event()
        self._failure: str | None = None
        self._bring_up_task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._slots = asyncio.Semaphore(_CONCURRENCY)
        #: Bumped on every boot. A guest copy of a workspace belongs to one generation;
        #: a reset or a restart means it is gone and has to be piped in again.
        self.generation = 0
        self._ready_hooks: list[Callable[[], Awaitable[None]]] = []
        self._hook_tasks: set[asyncio.Task[None]] = set()

    def on_ready(self, hook: Callable[[], Awaitable[None]]) -> None:
        """Run ``hook`` after every boot that comes up, so a service the VM lost on a reset
        can start itself again."""
        self._ready_hooks.append(hook)

    @property
    def available(self) -> bool:
        return self._ready.is_set()

    @property
    def building(self) -> bool:
        return not self.available and self._driver.phase == "building"

    @property
    def progress(self) -> str | None:
        """What a bring-up in progress is doing, worded for the operator."""
        if self.available or self._failure is not None:
            return None
        if self._driver.phase == "building":
            step = self._driver.step
            return "Building the VM image (first start after an update; can take 10–30 minutes)" + (
                f": {step}" if step else ""
            )
        if self._driver.phase == "booting":
            return "Booting"
        return None

    @property
    def failure(self) -> str | None:
        """Why the last bring-up failed, for the operator."""
        return self._failure

    async def start(self) -> None:
        """Begin bringing the VM up in the background."""
        if not self._enabled:
            self._failure = "the platform VM is disabled in settings"
            return
        self._bring_up_task = asyncio.create_task(self._bring_up(), name="platform-vm")

    async def wait_ready(self, timeout_s: float) -> bool:
        try:
            await asyncio.wait_for(self._ready.wait(), timeout_s)
        except TimeoutError:
            return False
        return True

    async def stop(self) -> None:
        if self._bring_up_task is not None:
            await cancel_and_wait(self._bring_up_task, STOP_TIMEOUT_S)
            self._bring_up_task = None
        async with self._lock:
            await self._take_down()

    async def reset(self) -> None:
        """Rebuild the VM from the clean image. Every guest copy of a workspace is lost
        and piped in again on next use; nothing durable lives in the guest."""
        if self._bring_up_task is not None:
            await cancel_and_wait(self._bring_up_task, STOP_TIMEOUT_S)
        async with self._lock:
            await self._take_down()
        await self.start()

    async def _bring_up(self) -> None:
        async with self._lock:
            self._failure = None
            try:
                await self._driver.boot()
                await self._install_scripts()
                await self._mount_cgroups()
                lay = self.layout
                await self._admin(
                    [
                        "sh",
                        "-c",
                        f"mkdir -p {lay.xfer} && nohup python3 {lay.opt}/relay.py "
                        f"--root {lay.root} --pool-port {lay.pool_port} "
                        f"--mux-port {lay.mux_port} --proxy-port {lay.proxy_port} "
                        f"> {lay.run_dir}/relay.log 2>&1 &",
                    ]
                )
                self._pool_port = await self._driver.forward(lay.pool_port)
                self._mux_port = await self._driver.forward(lay.mux_port)
                self._tunnel = EgressTunnel(self.gateway, self._connect_pool)
                self._tunnel.start()
            except VMError as exc:
                self._failure = str(exc)
                logger.warning("platform VM: unavailable: %s", exc)
                await self._take_down()
                return
            self.generation += 1
            self._ready.set()
            logger.info("platform VM: ready")
            for hook in self._ready_hooks:
                task = asyncio.create_task(hook())
                self._hook_tasks.add(task)
                task.add_done_callback(self._hook_tasks.discard)

    async def _take_down(self) -> None:
        self._ready.clear()
        if self._tunnel is not None:
            await self._tunnel.stop()
            self._tunnel = None
        try:
            await self._driver.stop()
        except VMError:
            logger.warning("platform VM: stopping failed", exc_info=True)

    async def _install_scripts(self) -> None:
        lay = self.layout
        await self._admin(
            ["mkdir", "-p", lay.opt, lay.path("run/odysseus/previews"), lay.path("work")]
        )
        for script in sorted(GUEST_SCRIPTS.glob("*.py")):
            await self._driver.put_file(script, f"{lay.opt}/{script.name}")
        await self._admin(["chmod", "-R", "a+rX", lay.opt])

    async def _mount_cgroups(self) -> None:
        """Mount cgroup v2 where the guest's init did not, so each workspace's caps hold.
        Without it commands run uncapped, inside the VM's own limits."""
        result = await self._driver.exec(
            [
                "sh",
                "-c",
                "grep -q ' /sys/fs/cgroup cgroup2 ' /proc/mounts"
                " || mount -t cgroup2 none /sys/fs/cgroup",
            ],
            timeout_s=_ADMIN_TIMEOUT_S,
        )
        if result.exit_code != 0:
            logger.warning(
                "platform VM: no cgroup v2, so workspaces run without their own caps: %s",
                result.stderr.strip(),
            )

    async def _connect_pool(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        if self._pool_port is None:
            raise OSError("the egress tunnel is not forwarded")
        return await asyncio.open_connection("127.0.0.1", self._pool_port)

    async def _connect_mux(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        if self._mux_port is None:
            raise OSError("the preview mux is not forwarded")
        return await asyncio.open_connection("127.0.0.1", self._mux_port)

    @property
    def _confine(self) -> str:
        return f"{self.layout.opt}/confine.py"

    @property
    def _files(self) -> str:
        return f"{self.layout.opt}/files.py"

    def _require(self) -> None:
        if not self._ready.is_set():
            raise VMError(self._failure or "the platform VM is still starting")

    async def _call(self, argv: list[str], *, timeout_s: float) -> ExecResult:
        self._require()
        async with self._slots:
            return await self._driver.exec(argv, timeout_s=timeout_s)

    async def _admin(self, argv: list[str], *, timeout_s: float = _ADMIN_TIMEOUT_S) -> str:
        """Run a platform-side command (not the agent's) and fail loudly if it fails."""
        async with self._slots:
            result = await self._driver.exec(argv, timeout_s=timeout_s)
        if result.exit_code != 0:
            raise VMError(
                f"`{shlex.join(argv)[:200]}` failed in the platform VM: "
                f"{(result.stderr or result.stdout).strip()[-500:]}"
            )
        return result.stdout

    # --- workspace commands -------------------------------------------------------

    async def run(
        self,
        key: str,
        argv: Sequence[str],
        *,
        env: Mapping[str, str],
        timeout_s: float,
        stdin: str | None = None,
        workdir: str = "",
    ) -> ExecResult:
        """Run the agent's command confined to workspace ``key``. Exit 124 is a timeout."""
        self._require()
        stdin_path = ""
        if stdin is not None:
            stdin_path = await self._put_bytes(stdin.encode())
        try:
            return await self._call(
                [
                    "python3",
                    self._confine,
                    "run",
                    "--root",
                    self.layout.root,
                    "--key",
                    key,
                    "--uid",
                    str(SANDBOX_UID),
                    "--timeout",
                    str(timeout_s),
                    *(["--stdin", stdin_path] if stdin_path else []),
                    *(["--workdir", workdir] if workdir else []),
                    *self.limits.flags(),
                    *_env_flags(env),
                    "--",
                    *argv,
                ],
                timeout_s=timeout_s + _EXEC_GRACE_S,
            )
        finally:
            if stdin_path:
                await self._call(["rm", "-f", stdin_path], timeout_s=_ADMIN_TIMEOUT_S)

    async def start_job(
        self,
        key: str,
        name: str,
        argv: Sequence[str],
        *,
        env: Mapping[str, str],
        timeout_s: float = 0,
        workdir: str = "",
        publish: int = 0,
        cwd_file: str = "",
    ) -> None:
        """Start the agent's command as a named background job confined to workspace
        ``key``; read it with :meth:`job` and end it with :meth:`stop_job`."""
        self._require()
        await self._admin(
            [
                "python3",
                self._confine,
                "start",
                "--root",
                self.layout.root,
                "--key",
                key,
                "--name",
                name,
                "--uid",
                str(SANDBOX_UID),
                *(["--timeout", str(timeout_s)] if timeout_s else []),
                *(["--workdir", workdir] if workdir else []),
                *(["--publish", str(publish)] if publish else []),
                *(["--cwd-file", cwd_file] if cwd_file else []),
                *self.limits.flags(),
                *_env_flags(env),
                "--",
                *argv,
            ]
        )

    async def stop_job(self, name: str) -> None:
        if not self.available:
            return
        await self._admin(
            ["python3", self._confine, "stop", "--root", self.layout.root, "--name", name]
        )

    async def job(self, name: str, *, out_offset: int = 0, err_offset: int = 0) -> JobState:
        """A job's state, and what it printed after the given offsets."""
        result = await self._call(
            [
                "python3",
                self._confine,
                "output",
                "--root",
                self.layout.root,
                "--name",
                name,
                "--out-offset",
                str(out_offset),
                "--err-offset",
                str(err_offset),
            ],
            timeout_s=_ADMIN_TIMEOUT_S,
        )
        try:
            raw = json.loads(result.stdout)
            return JobState(
                running=bool(raw["running"]),
                exit_code=raw["exit"] if isinstance(raw["exit"], int) else None,
                stdout=str(raw["out"]),
                stderr=str(raw["err"]),
                out_end=int(raw["out_end"]),
                err_end=int(raw["err_end"]),
            )
        except (ValueError, KeyError, TypeError) as exc:
            raise VMError(f"could not read job {name!r} in the platform VM") from exc

    async def read_guest(self, guest: str, *, max_bytes: int) -> str:
        """A small guest file's text, or "" when it is missing."""
        result = await self._call(["head", "-c", str(max_bytes), guest], timeout_s=_ADMIN_TIMEOUT_S)
        return result.stdout if result.exit_code == 0 else ""

    async def open_preview(self, key: str) -> PreviewPort:
        """A host loopback port reaching the server ``key``'s preview publishes."""
        self._require()
        port = PreviewPort(key, self._connect_mux)
        await port.open()
        return port

    # --- platform services --------------------------------------------------------

    async def start_service(self, name: str, user: str, argv: Sequence[str]) -> None:
        """Run a platform service as ``user``, with no capabilities, on the guest's own
        network, where it can reach the platform proxy. It gets PID and IPC namespaces of
        its own, and the workspaces and the platform's runtime files are hidden from it."""
        lay = self.layout
        command = shlex.join(
            [
                "bwrap",
                "--unshare-pid",
                "--unshare-ipc",
                "--new-session",
                "--dev-bind",
                "/",
                "/",
                "--proc",
                "/proc",
                "--tmpfs",
                lay.path("work"),
                "--tmpfs",
                lay.run_dir,
                "--perms",
                "1777",
                "--tmpfs",
                "/tmp",
                "--perms",
                "1777",
                "--tmpfs",
                "/dev/shm",
                "--",
                "setpriv",
                f"--reuid={user}",
                f"--regid={user}",
                "--init-groups",
                "--no-new-privs",
                "--inh-caps=-all",
                "--bounding-set=-all",
                "--",
                "env",
                f"HOME=/home/{user}",
                *argv,
            ]
        )
        run = self.layout.run_dir
        await self._admin(
            [
                "sh",
                "-c",
                f"mkdir -p {run}/logs && nohup setsid {command} "
                f"> {run}/logs/{name}.log 2>&1 & echo $! > {run}/{name}.pid",
            ]
        )

    async def stop_service(self, name: str) -> None:
        if not self.available:
            return
        pid = f"{self.layout.run_dir}/{name}.pid"
        await self._admin(["sh", "-c", f"[ -f {pid} ] && kill -- -$(cat {pid}); rm -f {pid}; true"])

    async def put_text(self, guest: str, text: str, *, mode: str = "0644") -> None:
        staged = await self._put_bytes(text.encode())
        await self._admin(["install", "-D", "-m", mode, staged, guest])
        await self._admin(["rm", "-f", staged])

    async def forward(self, guest_port: int) -> int:
        self._require()
        return await self._driver.forward(guest_port)

    # --- moving a workspace -------------------------------------------------------

    async def manifest(self, key: str) -> Manifest:
        result = await self._call(
            ["python3", self._files, "manifest", self.layout.work(key)],
            timeout_s=_ADMIN_TIMEOUT_S,
        )
        if result.exit_code != 0:
            raise VMError(f"could not list a workspace in the platform VM: {result.stderr}")
        try:
            return parse_manifest(json.loads(result.stdout or "{}"))
        except (ValueError, TypeError) as exc:
            raise VMError("the platform VM returned an unreadable workspace listing") from exc

    async def push(self, key: str, root: Path, changes: Changes) -> None:
        """Copy ``changes`` from the host workspace into the guest's copy."""
        self._require()
        local = self._local_xfer()
        try:
            await asyncio.to_thread(pack, root, changes.changed, local)
            archive = await self._put_path(local)
            listing = await self._put_bytes(json.dumps(changes.gone).encode())
            await self._admin(
                [
                    "python3",
                    self._files,
                    "unpack",
                    self.layout.work(key),
                    archive,
                    listing,
                    str(SANDBOX_UID),
                ],
                timeout_s=_ADMIN_TIMEOUT_S * 5,
            )
            await self._admin(["rm", "-f", archive, listing])
        finally:
            local.unlink(missing_ok=True)

    async def pull(self, key: str, root: Path, changes: Changes, *, project: bool = False) -> None:
        """Copy ``changes`` from the guest's copy back to the host workspace; ``project``
        applies the stricter checks for an operator's project folder."""
        self._require()
        listing = await self._put_bytes(json.dumps(changes.changed).encode())
        archive = f"{self.layout.xfer}/{secrets.token_hex(8)}.tar"
        local = self._local_xfer()
        try:
            await self._admin(
                ["python3", self._files, "pack", self.layout.work(key), listing, archive],
                timeout_s=_ADMIN_TIMEOUT_S * 5,
            )
            await self._driver.get_file(archive, local, max_bytes=self._transfer_max_bytes)
            await asyncio.to_thread(
                partial(
                    unpack,
                    root,
                    local,
                    changes.gone,
                    max_bytes=self._transfer_max_bytes,
                    project=project,
                )
            )
        finally:
            local.unlink(missing_ok=True)
            if self.available:
                await self._call(["rm", "-f", archive, listing], timeout_s=_ADMIN_TIMEOUT_S)

    async def wipe(self, key: str) -> None:
        if not self.available:
            return
        await self._admin(["python3", self._files, "wipe", self.layout.work(key)])

    def _local_xfer(self) -> Path:
        return self.scratch_path(".tar")

    def scratch_path(self, suffix: str = "") -> Path:
        """A fresh host path for a transfer; the caller removes it."""
        self._scratch.mkdir(parents=True, exist_ok=True)
        return self._scratch / f"{secrets.token_hex(8)}{suffix}"

    async def call(self, argv: list[str], *, timeout_s: float = _ADMIN_TIMEOUT_S) -> ExecResult:
        """A platform-side command whose exit status the caller reads."""
        return await self._call(argv, timeout_s=timeout_s)

    async def call_checked(self, argv: list[str], *, what: str) -> str:
        """A platform-side command that must succeed; its stdout."""
        result = await self._call(argv, timeout_s=_ADMIN_TIMEOUT_S * 5)
        if result.exit_code != 0:
            raise VMError(f"could not {what} in the platform VM: {result.stderr.strip()[-500:]}")
        return result.stdout

    async def put_path(self, local: Path) -> str:
        """Copy a host file into the guest's transfer area; its guest path."""
        self._require()
        return await self._put_path(local)

    async def fetch_file(self, guest: str, local: Path, *, max_bytes: int) -> None:
        self._require()
        async with self._slots:
            await self._driver.get_file(guest, local, max_bytes=max_bytes)

    async def _put_bytes(self, data: bytes) -> str:
        local = self._scratch / secrets.token_hex(8)
        self._scratch.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(local.write_bytes, data)
        try:
            return await self._put_path(local)
        finally:
            local.unlink(missing_ok=True)

    async def _put_path(self, local: Path) -> str:
        guest = f"{self.layout.xfer}/{secrets.token_hex(8)}"
        async with self._slots:
            await self._driver.put_file(local, guest)
        return guest

    def clear_scratch(self) -> None:
        shutil.rmtree(self._scratch, ignore_errors=True)


def _env_flags(env: Mapping[str, str]) -> list[str]:
    flags: list[str] = []
    for name, value in env.items():
        flags += ["--env", f"{name}={value}"]
    return flags
