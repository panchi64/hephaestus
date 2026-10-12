"""What the platform needs from a virtual machine, and nothing more.

Every call is driven by the host; the guest has no way to call back. :class:`VMDriver`
is the seam the smolvm backend implements and tests replace.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from core.exceptions import OdysseusError


class VMError(OdysseusError):
    """The VM itself failed: it would not boot, stopped answering, or a transfer broke.
    A command's non-zero exit is an :class:`ExecResult`, not this."""


@dataclass(frozen=True)
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str


class VMDriver(Protocol):
    async def boot(self) -> None:
        """Start a fresh VM from the clean image, ready for :meth:`exec`."""

    async def stop(self) -> None:
        """Stop the VM and discard its disk."""

    async def alive(self) -> bool:
        """Whether the VM is running and answers a command."""

    async def exec(self, argv: list[str], *, timeout_s: float) -> ExecResult:
        """Run ``argv`` as root in the guest."""

    async def put_file(self, local: Path, guest: str) -> None:
        """Copy one host file into the guest."""

    async def get_file(self, guest: str, local: Path, *, max_bytes: int) -> None:
        """Copy one guest file to the host, refusing one larger than ``max_bytes``."""

    async def forward(self, guest_port: int) -> int:
        """Expose a guest TCP port on host loopback and return the host port."""
