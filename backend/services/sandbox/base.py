"""The execution-sandbox capability — value types and errors.

Agent code runs in the conversation's VM (:mod:`services.vm`), which mounts the host
folders live. It cannot read or modify other host files, processes or environment, and
reaches only the installation's egress allowlist plus the conversation's grants.

Not everything the agent runs arrives there: the approval-gated host command executes
on the operator's own machine under OS-level confinement (:mod:`services.sandbox.fence`),
which fences the same domains and hides the credential paths, but is a different
boundary.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from core.exceptions import HephaestusError

_SAFE = re.compile(r"[^A-Za-z0-9_.-]")


def safe_key(key: str) -> str:
    """A directory-safe token for a workspace key (leading char guaranteed).

    The egress policy derives a workspace's allowlist directory from the same key, so both
    must share this spelling.
    """
    return "s" + _SAFE.sub("-", key)


class SandboxError(HephaestusError):
    """The sandbox failed to run the request (not a non-zero exit — that is an
    ordinary :class:`SandboxResult` the agent acts on). This is an infrastructure
    failure: the VM is down or still starting, or a workspace could not cross into it."""


class HostExecutionError(HephaestusError):
    """The host command could not be launched (a non-zero exit is a normal
    :class:`SandboxResult`, not this).

    Lives here because both ``process.py`` and ``fence.py`` raise it and ``process.py``
    imports ``fence.py``."""


def contained_path(root: Path, relpath: str, *, what: str = "path") -> Path:
    """Resolve ``relpath`` under ``root``, refusing anything that escapes it.

    Raises rather than dropping the path, so a traversal attempt is an error and not a
    missing output. ``root`` is resolved too, so a symlinked workspace doesn't read as an
    escape from itself.
    """
    resolved_root = root.resolve()
    target = (root / relpath).resolve()
    if not target.is_relative_to(resolved_root):
        raise SandboxError(f"{what} escapes the sandbox workspace: {relpath!r}")
    return target


def contained_file(root: Path, relpath: str) -> Path | None:
    """:func:`contained_path` as ``None`` instead of an exception, and only for an existing file.

    For the file picker and the turn composer, where an unresolvable path is an ordinary
    outcome of operator typing. A directory is refused too: its read tool cannot open it.
    """
    if not relpath or relpath.startswith("/") or "\0" in relpath:
        return None
    try:
        target = contained_path(root, relpath, what="file reference")
        return target if target.is_file() else None
    except SandboxError, OSError:
        return None


@dataclass(frozen=True)
class SandboxSpec:
    """One isolated execution request.

    ``command`` is an argv run in the workspace (e.g. ``["python", "-c", code]`` or
    ``["bash", "-c", script]``). ``env`` is added to the sandbox's own environment —
    the host environment never leaks in.
    """

    command: Sequence[str]
    stdin: str | None = None
    env: Mapping[str, str] = field(default_factory=dict[str, str])
    timeout_s: float = 30.0


@dataclass(frozen=True)
class SandboxResult:
    """What an isolated run produced. A non-zero ``exit_code`` or a ``timed_out``
    run is a normal result the agent reasons about — not an error."""

    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False
    #: What the agent should know about bringing its changes back, such as a VM restart.
    notes: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out
