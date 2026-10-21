"""One workspace's guest copy, kept in step with the host copy.

The host directory is the workspace. Before a command runs, whatever changed on the host
since the last sync (a file tool's edit, an attachment) is pushed in; after it runs,
whatever changed in the guest is pulled back. When the workspace goes idle the guest copy
is pulled one last time and wiped, so the VM holds only the workspaces in use right now.

Not safe for concurrent use; its owner serializes calls.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from .host import WorkspaceHost
from .sync import Manifest, diff, scan

#: What the agent is told when a bring-out finds the guest copy gone with its VM.
LOST_TO_RESTART = "the VM restarted; changes made in it since the last command were lost"


class GuestWorkspace:
    def __init__(self, vm: WorkspaceHost, key: str, root: Path) -> None:
        self._vm: WorkspaceHost = vm
        self.key: str = key
        self.root: Path = root
        self._host: Manifest = {}
        self._guest: Manifest = {}
        self._generation: int | None = None

    @property
    def is_in(self) -> bool:
        """Whether the guest holds a current copy."""
        return self._generation == self._vm.generation and self._vm.available

    async def bring_in(self) -> None:
        """Make the guest copy match the host copy."""
        host_now = await asyncio.to_thread(scan, self.root)
        if not self.is_in:
            self._host, self._guest = {}, {}
            await self._vm.wipe(self.key)
        changes = diff(host_now, self._host)
        if changes or not self.is_in:
            await self._vm.push(self.key, self.root, changes)
            self._guest = await self._vm.manifest(self.key)
        self._host = host_now
        self._generation = self._vm.generation

    async def bring_out(self) -> str | None:
        """Copy what changed in the guest back to the host. Returns a note when the copy
        belongs to an older VM generation, whose changes are gone."""
        if not self.is_in:
            return LOST_TO_RESTART if self._generation is not None else None
        guest_now = await self._vm.manifest(self.key)
        changes = diff(guest_now, self._guest)
        if changes:
            await self._vm.pull(self.key, self.root, changes)
        self._guest = guest_now
        self._host = await asyncio.to_thread(scan, self.root)
        return None

    async def evict(self) -> None:
        """Bring the work out, then wipe the guest copy."""
        if self.is_in:
            _ = await self.bring_out()
            await self._vm.wipe(self.key)
        self._generation = None
        self._host, self._guest = {}, {}

    def forget(self) -> None:
        """Drop the guest copy's bookkeeping without touching either side."""
        self._generation = None
        self._host, self._guest = {}, {}
