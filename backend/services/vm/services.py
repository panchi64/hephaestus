"""The services machine: one long-lived machine for headless Chromium and SearXNG.

It mounts nothing from the host and reaches only public addresses (``public_cidrs``), so
a page it loads can't reach the host's own services or the LAN. Each service runs as its
own user and is reached from the host through a published port. A reset boots a fresh
machine from the image; the services start themselves again from :meth:`on_ready` hooks.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import posixpath
import shlex
from collections.abc import Callable, Coroutine, Sequence
from contextlib import suppress
from functools import cache

import smol

from .errors import VMError
from .platform import Platform
from .target import LOCAL

logger = logging.getLogger(__name__)

_RUN_DIR = "/run/hephaestus"
_ADMIN_TIMEOUT_S = 60.0
#: Everything a public address is not: private, loopback, link-local, carrier-grade NAT,
#: benchmarking, multicast and reserved ranges.
_NOT_PUBLIC = (
    "0.0.0.0/8",
    "10.0.0.0/8",
    "100.64.0.0/10",
    "127.0.0.0/8",
    "169.254.0.0/16",
    "172.16.0.0/12",
    "192.0.0.0/24",
    "192.168.0.0/16",
    "198.18.0.0/15",
    "224.0.0.0/3",
)

type ReadyHook = Callable[[], Coroutine[object, object, None]]


@cache
def public_cidrs() -> tuple[str, ...]:
    """The IPv4 internet minus :data:`_NOT_PUBLIC`, as smol's ``allow_cidrs`` takes it."""
    networks = [ipaddress.IPv4Network("0.0.0.0/0")]
    for text in _NOT_PUBLIC:
        blocked = ipaddress.IPv4Network(text)
        kept: list[ipaddress.IPv4Network] = []
        for network in networks:
            if blocked.subnet_of(network):
                kept.extend(network.address_exclude(blocked))
            elif not network.subnet_of(blocked):
                kept.append(network)
        networks = kept
    return tuple(str(network) for network in sorted(networks))


class ServicesMachine:
    """Implements ``ServiceHost``, plus the state the operator sees."""

    def __init__(self, platform: Platform, *, name: str, cpus: int, memory_mb: int) -> None:
        self._platform: Platform = platform
        self._name: str = name
        self._cpus: int = cpus
        self._memory_mb: int = memory_mb
        self._machine: smol.AsyncMachine | None = None
        self._ready: asyncio.Event = asyncio.Event()
        self._failure: str | None = None
        self._task: asyncio.Task[None] | None = None
        self._lock: asyncio.Lock = asyncio.Lock()
        self._ports: dict[int, int] = {}
        self._hooks: list[ReadyHook] = []
        self._hook_tasks: set[asyncio.Task[None]] = set()

    def on_ready(self, hook: ReadyHook) -> None:
        """Run ``hook`` after every boot, so a service a reset ended starts again."""
        self._hooks.append(hook)

    @property
    def available(self) -> bool:
        return self._ready.is_set()

    @property
    def building(self) -> bool:
        return not self.available and self._platform.image.building

    @property
    def failure(self) -> str | None:
        return self._failure or self._platform.failure

    @property
    def progress(self) -> str | None:
        if self.available or self.failure is not None:
            return None
        return self._platform.progress or "Booting"

    async def responds(self) -> bool:
        machine = self._machine
        if machine is None or not self.available:
            return False
        try:
            result = await machine.exec(["true"], smol.ExecOptions(timeout=10))
        except smol.SmolError:
            return False
        return result.exit_code == 0

    def mark_unavailable(self, reason: str) -> None:
        self._ready.clear()
        self._failure = reason

    async def start(self) -> None:
        self._task = asyncio.create_task(self._bring_up(), name="services-machine")

    async def stop(self) -> None:
        if self._task is not None:
            _ = self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        async with self._lock:
            await self._take_down()

    async def reset(self) -> None:
        await self.stop()
        await self.start()

    async def wait_ready(self, timeout_s: float) -> bool:
        if self._ready.is_set():
            return True
        task = self._task
        if task is None or task.done():
            return False
        ready = asyncio.ensure_future(self._ready.wait())
        try:
            _ = await asyncio.wait({ready, task}, timeout=timeout_s, return_when="FIRST_COMPLETED")
        finally:
            _ = ready.cancel()
        return self._ready.is_set()

    async def put_text(self, guest: str, text: str, *, mode: str = "0644") -> None:
        machine = self._require()
        _ = await self._sh(f"mkdir -p {shlex.quote(posixpath.dirname(guest))}")
        try:
            await machine.write_file(guest, text, int(mode, 8))
        except smol.SmolError as exc:
            raise VMError(f"could not write {guest}: {exc}") from exc

    async def start_service(self, name: str, user: str, argv: Sequence[str]) -> None:
        """Start ``argv`` as ``user`` in the background, logging to the service's log."""
        await self.stop_service(name)
        command = (
            f"mkdir -p {_RUN_DIR} && chmod 1777 {_RUN_DIR} && "
            + f"su -s /bin/sh {shlex.quote(user)} -c "
            + shlex.quote(
                f"setsid {shlex.join(argv)} > {_log(name)} 2>&1 < /dev/null & "
                + f"echo $! > {_pid(name)}"
            )
        )
        result = await self._sh(command)
        if result.exit_code != 0:
            raise VMError(f"could not start {name}: {result.stderr.strip()}")

    async def stop_service(self, name: str) -> None:
        _ = await self._sh(
            f'[ -f {_pid(name)} ] && kill -TERM -- -"$(cat {_pid(name)})" 2>/dev/null; '
            + f"rm -f {_pid(name)}; true"
        )

    async def service_alive(self, name: str) -> bool:
        result = await self._sh(f'kill -0 "$(cat {_pid(name)})" 2>/dev/null')
        return result.exit_code == 0

    async def service_log(self, name: str, *, max_bytes: int) -> str:
        return (await self._sh(f"tail -c {max_bytes} {_log(name)} 2>/dev/null")).stdout

    async def forward(self, guest_port: int) -> int:
        """The host loopback port that reaches ``guest_port``."""
        host = self._ports.get(guest_port)
        if host is None:
            try:
                host = (await self._require().publish(guest_port)).host
            except smol.SmolError as exc:
                raise VMError(f"could not publish port {guest_port}: {exc}") from exc
            if host is None:
                raise VMError(f"port {guest_port} was published without a host port")
            self._ports[guest_port] = host
        return host

    def _require(self) -> smol.AsyncMachine:
        if self._machine is None:
            raise VMError(self.failure or "the services machine is still starting")
        return self._machine

    async def _sh(self, command: str) -> smol.ExecResult:
        try:
            return await self._require().exec(
                ["sh", "-c", command], smol.ExecOptions(timeout=_ADMIN_TIMEOUT_S)
            )
        except smol.SmolError as exc:
            raise VMError(f"the services machine did not answer: {exc}") from exc

    async def _bring_up(self) -> None:
        async with self._lock:
            self._failure = None
            if not await self._platform.wait_ready(float("inf")):
                return
            try:
                with suppress(smol.SmolError):
                    await smol.AsyncMachine.delete_by_name(self._name, LOCAL)
                self._machine = await smol.AsyncMachine.create(
                    smol.MachineConfig(
                        name=self._name,
                        image=self._platform.require(),
                        resources=smol.ResourceSpec(
                            cpus=self._cpus,
                            memory_mb=self._memory_mb,
                            allow_cidrs=list(public_cidrs()),
                        ),
                        persistent=True,
                    ),
                    LOCAL,
                )
            except smol.SmolError as exc:
                self._failure = f"the services machine did not start: {exc}"
                logger.warning("vm: %s", self._failure)
                return
            self._ready.set()
            logger.info("vm: services machine ready")
            for hook in self._hooks:
                task = asyncio.create_task(hook())
                self._hook_tasks.add(task)
                task.add_done_callback(self._hook_tasks.discard)

    async def _take_down(self) -> None:
        self._ready.clear()
        self._ports.clear()
        machine, self._machine = self._machine, None
        if machine is not None:
            with suppress(smol.SmolError):
                _ = await machine.delete()


def _pid(name: str) -> str:
    return f"{_RUN_DIR}/{name}.pid"


def _log(name: str) -> str:
    return f"{_RUN_DIR}/{name}.log"
