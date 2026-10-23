"""What a shell command hands back: structure, not a rendered string.

A run returns :class:`ShellResult` and a background command :class:`BackgroundStatus`.
The prose a model reads is composed one layer up (``tools/shell.py``).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

#: Called with everything a running command has newly printed and how long it has been
#: running. One string per tick, stdout ahead of stderr, since nothing records how the two
#: files interleaved.
type ProgressHook = Callable[[str, float], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class ShellResult:
    """One finished command, as fields.

    ``exit_code`` is None only when the command was killed on its timeout, since a zero
    would read as success. ``duration_ms`` is wall clock around the spawn. A timed-out
    command keeps what it printed.
    """

    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool
    duration_ms: int

    @property
    def ok(self) -> bool:
        """Whether the command itself succeeded — ran to completion, and exited zero."""
        return not self.timed_out and self.exit_code == 0


@dataclass(frozen=True, slots=True)
class BackgroundStatus:
    """A background command as it stands: how far it has got, and everything it printed.

    ``exit_code`` is None while it is still running, and after a stop that had to
    terminate a process that had not chosen its own status.
    """

    command_id: str
    status: Literal["running", "finished", "stopped"]
    exit_code: int | None
    stdout: str
    stderr: str
    #: Whether a VM command was given the network; None for a host command.
    network: bool | None = None
    #: What the agent should know about bringing its changes back, such as a VM restart.
    notes: tuple[str, ...] = ()
