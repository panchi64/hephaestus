"""One smol machine per conversation, booted from the guest image on its first command.

A machine mounts the conversation's home live: its scratch, its projects (read-only unless
the thread may write them) and its worktrees, at the paths the file tools use, so a file a
tool writes on the host is the file the next command sees. Its network is the
conversation's egress allowlist, enforced by smol; no allowlist, or offline mode, means no
network at all.

What a machine mounts and reaches is fixed when it boots, so a change to either (a lease,
an egress grant, going offline) restarts it, which ends the commands it was running. A
machine left idle is deleted; the host folders it mounted keep everything it wrote.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field

import smol
from pydantic_ai_harness.smol_sandbox import SmolSandboxBackend

from core.periodic import PeriodicTask

from .errors import MachineUnavailableError
from .platform import Platform
from .target import LOCAL

logger = logging.getLogger(__name__)

_SAFE = re.compile(r"[^a-z0-9-]")

#: How long a restart for new access waits for the commands in flight; past it the caller
#: is told to retry rather than held behind the key's lock.
_RESTART_WAIT_S = 60.0

#: `~` is the agent's home, so its paths mean the same in a command as in a file tool, and
#: packages install into the scratch, which outlives the machine.
_GUEST_ENV = (
    ("HOME", "/home/agent"),
    (
        "PATH",
        "/home/agent/scratch/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    ),
    ("LANG", "C.UTF-8"),
    ("PYTHONUSERBASE", "/home/agent/scratch/.local"),
    ("PIP_USER", "1"),
    ("PIP_BREAK_SYSTEM_PACKAGES", "1"),
)


@dataclass(frozen=True, slots=True)
class Bind:
    """A host folder at ``target`` in the machine."""

    source: str
    target: str
    read_only: bool


@dataclass(frozen=True, slots=True)
class MachineSpec:
    """What a machine mounts and reaches; a machine whose spec changes is restarted."""

    binds: tuple[Bind, ...]
    #: The hosts it may reach, or None for no network.
    allow_hosts: tuple[str, ...] | None


@dataclass
class _Slot:
    spec: MachineSpec
    machine: smol.AsyncMachine
    last_used: float
    busy: int = 0
    idle: asyncio.Event = field(default_factory=asyncio.Event)
    #: One backend per working directory: each opens its own handle to the machine.
    backends: dict[str, SmolSandboxBackend] = field(default_factory=dict)

    def backend(self, working_dir: str) -> SmolSandboxBackend:
        backend = self.backends.get(working_dir)
        if backend is None:
            backend = SmolSandboxBackend(sandbox=self.machine, working_dir=working_dir)
            self.backends[working_dir] = backend
        return backend


class ConversationMachines:
    """Each conversation's machine, created on first use and reaped when idle."""

    def __init__(
        self,
        platform: Platform,
        *,
        prefix: str,
        cpus: int,
        memory_mb: int,
        idle_ttl_s: float,
        reap_interval_s: float,
        max_machines: int,
    ) -> None:
        self._platform: Platform = platform
        self._prefix: str = prefix
        self._cpus: int = cpus
        self._memory_mb: int = memory_mb
        self._idle_ttl_s: float = idle_ttl_s
        self._max: int = max_machines
        self._slots: dict[str, _Slot] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._end_hooks: list[Callable[[str], None]] = []
        self._reaper: PeriodicTask = PeriodicTask(
            "vm-reaper", interval_s=reap_interval_s, work=self.sweep, logger=logger
        )

    @property
    def platform(self) -> Platform:
        return self._platform

    def running(self, key: str) -> bool:
        return key in self._slots

    def machine(self, key: str) -> smol.AsyncMachine | None:
        """The conversation's machine, if it has one; hold it with :meth:`use`."""
        slot = self._slots.get(key)
        return slot.machine if slot is not None else None

    def touch(self, key: str) -> None:
        """Count as activity, so a machine only viewed is not reaped."""
        slot = self._slots.get(key)
        if slot is not None:
            slot.last_used = time.monotonic()

    def on_end(self, hook: Callable[[str], None]) -> None:
        """Call ``hook`` with the key of every machine that is deleted or restarted."""
        self._end_hooks.append(hook)

    async def start(self) -> None:
        await self._delete_orphans()
        await self._reaper.start()

    async def stop(self) -> None:
        await self._reaper.stop()
        await self.release_all()

    async def purge(self, key: str) -> None:
        """Delete a deleted conversation's machine and those of the agents it delegated to."""
        for held in list(self._slots):
            if held == key or held.startswith(f"{key}/"):
                await self.release(held)

    async def release_all(self) -> None:
        for key in list(self._slots):
            await self.release(key)

    @asynccontextmanager
    async def use(
        self, key: str, spec: MachineSpec, *, working_dir: str
    ) -> AsyncGenerator[SmolSandboxBackend]:
        """This conversation's machine, booted or restarted to match ``spec``, held busy
        (so neither the reaper nor a restart ends it) until the block exits."""
        slot = await self._acquire(key, spec)
        slot.busy += 1
        slot.idle.clear()
        try:
            yield slot.backend(working_dir)
        finally:
            slot.busy -= 1
            slot.last_used = time.monotonic()
            if slot.busy == 0:
                slot.idle.set()

    async def release(self, key: str) -> None:
        """Delete the conversation's machine, if it has one; its host folders stay."""
        async with self._lock(key):
            slot = self._slots.pop(key, None)
            if slot is not None:
                await self._end(key, slot)

    async def sweep(self) -> None:
        for key in list(self._slots):
            # Decided under the key's lock: a command acquiring the machine holds it until
            # the slot is marked busy.
            async with self._lock(key):
                slot = self._slots.get(key)
                if slot is None or slot.busy or not self._expired(slot):
                    continue
                logger.info("vm: reaping idle machine for %s", key)
                del self._slots[key]
                await self._end(key, slot)

    def _expired(self, slot: _Slot) -> bool:
        return time.monotonic() - slot.last_used >= self._idle_ttl_s

    def _lock(self, key: str) -> asyncio.Lock:
        return self._locks.setdefault(key, asyncio.Lock())

    async def _acquire(self, key: str, spec: MachineSpec) -> _Slot:
        async with self._lock(key):
            slot = self._slots.get(key)
            if slot is not None and slot.spec == spec:
                slot.last_used = time.monotonic()
                return slot
            if slot is not None:
                # A restart waits for the commands in flight rather than ending them.
                try:
                    _ = await asyncio.wait_for(slot.idle.wait(), _RESTART_WAIT_S)
                except TimeoutError as exc:
                    raise MachineUnavailableError(
                        "the VM needs a restart for new access but a command is still "
                        + "running in it; wait for it to finish or stop it, then retry"
                    ) from exc
                logger.info("vm: restarting the machine for %s with a new mount or egress", key)
                del self._slots[key]
                await self._end(key, slot)
            await self._make_room()
            machine = await self._create(key, spec)
            slot = _Slot(spec=spec, machine=machine, last_used=time.monotonic())
            slot.idle.set()
            self._slots[key] = slot
            return slot

    async def _make_room(self) -> None:
        while len(self._slots) >= self._max:
            # Chosen afresh each round, between awaits: a slot that turned busy while the
            # last machine was being deleted is no longer a candidate.
            idle = [(slot.last_used, key) for key, slot in self._slots.items() if not slot.busy]
            if not idle:
                raise MachineUnavailableError(
                    f"all {self._max} VMs are busy with other conversations; try again shortly"
                )
            _, key = min(idle)
            await self._end(key, self._slots.pop(key))

    async def _end(self, key: str, slot: _Slot) -> None:
        try:
            _ = await slot.machine.delete()
        except smol.SmolError:
            logger.warning("vm: could not delete the machine for %s", key, exc_info=True)
        for hook in self._end_hooks:
            try:
                hook(key)
            except Exception:  # one listener must not keep the others from hearing
                logger.exception("vm: an end hook failed for %s", key)

    async def _delete_orphans(self) -> None:
        """Delete this installation's machines a previous process left running."""
        try:
            summaries = await smol.AsyncMachine.list(LOCAL)
        except smol.SmolError:
            logger.warning("vm: could not list machines to clean up", exc_info=True)
            return
        for summary in summaries:
            if summary.name.startswith(self._name_prefix):
                with suppress(smol.SmolError):
                    await smol.AsyncMachine.delete_by_name(summary.name, LOCAL)

    @property
    def _name_prefix(self) -> str:
        """Every conversation machine's name starts so; the services machine's does not."""
        return f"{self._prefix}-c-"

    async def _create(self, key: str, spec: MachineSpec) -> smol.AsyncMachine:
        image = self._platform.require()
        name = _SAFE.sub("-", f"{self._name_prefix}{key}".lower())[:63]
        # A machine left by a process that died keeps the name; it is ours to replace.
        with suppress(smol.SmolError):
            await smol.AsyncMachine.delete_by_name(name, LOCAL)
        resources = smol.ResourceSpec(cpus=self._cpus, memory_mb=self._memory_mb)
        if spec.allow_hosts is not None:
            resources.allow_hosts = list(spec.allow_hosts)
        config = smol.MachineConfig(
            name=name,
            image=image,
            mounts=[
                smol.MountSpec(source=b.source, target=b.target, read_only=b.read_only)
                for b in spec.binds
            ],
            resources=resources,
            persistent=True,
            workdir="/home/agent",
            env=dict(_GUEST_ENV),
        )
        try:
            return await smol.AsyncMachine.create(config, LOCAL)
        except smol.SmolError as exc:
            raise MachineUnavailableError(f"the VM did not start: {exc}") from exc
