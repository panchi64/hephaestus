"""What a shell command hands back: structure, not a rendered string.

A run returns :class:`ShellResult` — exit code, the two streams apart, whether it was killed
on a timeout, and how long it took — and a background command returns
:class:`BackgroundStatus`. The prose a *model* reads is composed one layer up
(``tools/shell.py``) from these fields; nothing here decides how a result is worded.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

#: Called with everything a running command has newly printed and how long it has been
#: running. One string per tick, stdout ahead of stderr — the two are separate files and
#: nothing records how they interleaved. The finished :class:`ShellResult` is where they are
#: apart.
type ProgressHook = Callable[[str, float], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class ShellResult:
    """One finished command, as fields.

    ``exit_code`` is **None only when the command was killed on its timeout** — there was
    no status to collect, and a zero there would read as success. The streams are whole
    and unlabelled, ``duration_ms`` is wall clock measured around the spawn, and a command
    that printed nothing carries two empty strings rather than a sentence saying so. A
    timed-out command keeps what it managed to print.
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
