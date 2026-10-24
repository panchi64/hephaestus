"""The run's workspace: files on the host, commands in the conversation's machine.

The file half is the home's own backend (``backend.py``), so a file tool acts on the real
host folder at once and needs no machine. The command half runs in the conversation's
machine, which mounts the same folders at the same paths, so either half sees what the
other just wrote.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence

from pydantic_ai.workspaces import (
    CommandResult,
    FileEntry,
    WorkspaceCommand,
    WorkspaceRef,
    WorkspaceUnavailableError,
)

from services.vm.errors import MachineUnavailableError
from services.vm.machines import Bind, ConversationMachines, MachineSpec

from .paths import SCRATCH, SCRATCH_ALIAS
from .table import RunHome

type HomeResolver = Callable[[], Awaitable[RunHome]]
#: The hosts the conversation may reach now, or None for no network.
type EgressResolver = Callable[[], Awaitable[tuple[str, ...] | None]]


def machine_spec(home: RunHome, allow_hosts: tuple[str, ...] | None) -> MachineSpec:
    """What the conversation's machine mounts and reaches: every place of the home, at
    its path, read-only where the thread may not write it."""
    binds = [Bind(str(m.root), m.place.path, read_only=not m.writable) for m in home.mounts]
    scratch = home.mount(SCRATCH)
    if scratch is not None:
        binds.append(Bind(str(scratch.root), SCRATCH_ALIAS, read_only=False))
    return MachineSpec(binds=tuple(binds), allow_hosts=allow_hosts)


class HomeWorkspace:
    """Implements ``WorkspaceBackend``, ``SupportsFilesystem``, ``SupportsRealpath`` and
    ``SupportsCommands``. Building one does no I/O: the home resolves on first use."""

    def __init__(
        self,
        key: str,
        home: HomeResolver,
        egress: EgressResolver,
        machines: ConversationMachines | None,
    ) -> None:
        self._ref: WorkspaceRef = WorkspaceRef(provider="hephaestus-home", id=key)
        self._key: str = key
        self._home: HomeResolver = home
        self._egress: EgressResolver = egress
        self._machines: ConversationMachines | None = machines

    @property
    def ref(self) -> WorkspaceRef:
        return self._ref

    async def working_dir(self) -> str:
        return (await self._home()).default.path

    async def read_bytes(self, path: str) -> bytes:
        return await (await self._home()).backend.read_bytes(path)

    async def write_bytes(self, path: str, data: bytes) -> None:
        await (await self._home()).backend.write_bytes(path, data)

    async def stat(self, path: str) -> FileEntry:
        return await (await self._home()).backend.stat(path)

    async def list_dir(self, path: str) -> Sequence[FileEntry]:
        return await (await self._home()).backend.list_dir(path)

    async def make_dir(self, path: str) -> None:
        await (await self._home()).backend.make_dir(path)

    async def remove(self, path: str) -> None:
        await (await self._home()).backend.remove(path)

    async def exists(self, path: str) -> bool:
        return await (await self._home()).backend.exists(path)

    async def realpath(self, path: str) -> str:
        return await (await self._home()).backend.realpath(path)

    async def run(
        self,
        command: WorkspaceCommand,
        *,
        shell: bool = False,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> CommandResult:
        if self._machines is None:
            raise WorkspaceUnavailableError("commands are unavailable: the VM is disabled")
        home = await self._home()
        spec = machine_spec(home, await self._egress())
        try:
            async with self._machines.use(self._key, spec, working_dir=home.default.path) as vm:
                return await vm.run(command, shell=shell, env=env, timeout=timeout)
        except MachineUnavailableError as exc:
            raise WorkspaceUnavailableError(f"commands are unavailable: {exc}") from exc
