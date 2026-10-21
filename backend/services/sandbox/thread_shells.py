"""Every thread's shell, held for the life of the process.

A thread's shell carries what outlives a run: its working directory and the background
commands it started. The permission judge reads the same working directory the next
command starts in, so the two cannot disagree about where a relative path lands.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

from services.vm.project import ProjectCopies

from .home_shell import HomeShell
from .host_shell import HostShell

#: Idle shells kept past this are dropped oldest first; a busy one is never dropped.
_MAX_IDLE = 256


@dataclass
class ThreadShell:
    """One thread's shells: its home shell in the VM, and a host shell per host folder."""

    home: HomeShell
    default_timeout: float
    max_output_chars: int
    hosts: dict[Path, HostShell] = field(default_factory=dict)

    @property
    def busy(self) -> bool:
        return self.home.busy or any(host.busy for host in self.hosts.values())

    def host_at(self, root: Path) -> HostShell:
        shell = self.hosts.get(root)
        if shell is None:
            shell = HostShell(
                root,
                default_timeout=self.default_timeout,
                max_output_chars=self.max_output_chars,
            )
            self.hosts[root] = shell
        return shell

    async def shutdown(self, copies: ProjectCopies) -> None:
        await self.home.shutdown(copies)
        for host in self.hosts.values():
            await host.shutdown()


class ThreadShells:
    """The shell for each workspace key, built on first use."""

    def __init__(
        self, copies: ProjectCopies, *, default_timeout: float, max_output_chars: int
    ) -> None:
        self._copies: ProjectCopies = copies
        self._default_timeout: float = default_timeout
        self._max_output_chars: int = max_output_chars
        self._shells: OrderedDict[str, ThreadShell] = OrderedDict()

    def get(self, key: str, *, start: str) -> ThreadShell:
        """The thread's shell, built standing in ``start`` the first time."""
        shell = self._shells.pop(key, None)
        if shell is None:
            shell = ThreadShell(
                home=HomeShell(
                    default_timeout=self._default_timeout,
                    max_output_chars=self._max_output_chars,
                    start=start,
                ),
                default_timeout=self._default_timeout,
                max_output_chars=self._max_output_chars,
            )
        self._shells[key] = shell
        self._drop_idle()
        return shell

    def cwd(self, key: str) -> str | None:
        """Where the thread's next command starts, or None before its first."""
        shell = self._shells.get(key)
        return shell.home.cwd if shell is not None else None

    async def close(self, key: str) -> None:
        """Stop what the thread left running and forget its shell."""
        shell = self._shells.pop(key, None)
        if shell is not None:
            await shell.shutdown(self._copies)

    async def stop(self) -> None:
        for key in list(self._shells):
            await self.close(key)

    def _drop_idle(self) -> None:
        idle = [key for key, shell in self._shells.items() if not shell.busy]
        for key in idle[: max(0, len(idle) - _MAX_IDLE)]:
            del self._shells[key]
