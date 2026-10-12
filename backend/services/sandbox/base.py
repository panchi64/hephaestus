"""The execution-sandbox capability — value types and errors.

Agent code runs in the platform VM (:mod:`services.vm`), confined to a guest copy of
its own workspace. It cannot read or modify the host's files, processes or environment,
and reaches the network only through the egress gateway, held to its workspace's
allowlist. Outputs return explicitly (stdout/stderr, and the workspace's files piped back
out); nothing escapes as a side effect.

Not everything the agent runs arrives there: the approval-gated host command executes
on the operator's own machine under OS-level confinement (:mod:`services.sandbox.host`),
which fences the same domains and hides the credential paths, but is a different
boundary.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from core.exceptions import OdysseusError

_SAFE = re.compile(r"[^A-Za-z0-9_.-]")


def safe_key(key: str) -> str:
    """A directory-safe token for a workspace key (leading char guaranteed).

    Here rather than beside the session manager because the key names more than a
    workspace directory: the egress policy derives a workspace's allowlist directory from
    the same key, and a second spelling of "safe" would file an allowlist under a
    different name from the workspace.
    """
    return "s" + _SAFE.sub("-", key)


class SandboxError(OdysseusError):
    """The sandbox failed to run the request (not a non-zero exit — that is an
    ordinary :class:`SandboxResult` the agent acts on). This is an infrastructure
    failure: the VM is down or still starting, or a workspace could not cross into it."""


class HostExecutionError(OdysseusError):
    """The host command could not be launched (a non-zero exit is a normal
    :class:`SandboxResult`, not this).

    Down here with the other error rather than beside the runner that raises it most
    (``process.py``), because the *fence* raises it too — a scratch directory someone else
    planted refuses the command before a process is ever started — and ``process.py``
    imports ``host.py`` for the fence. One of the two had to stop owning the name, and an
    error type is exactly what this module is for."""


def contained_path(root: Path, relpath: str, *, what: str = "path") -> Path:
    """Resolve ``relpath`` under ``root``, refusing anything that escapes it.

    The one place this check lives. It was written out at each of the four call
    sites — staging inputs, copying outputs, and the session's read/write — and had
    already drifted: three raised on an escape and the fourth silently dropped the
    file, which turns a traversal attempt into a missing output rather than an error.
    A containment check that fails quietly is not a containment check.

    ``root`` is resolved too, so a symlinked workspace doesn't read as an escape from
    itself.
    """
    resolved_root = root.resolve()
    target = (root / relpath).resolve()
    if not target.is_relative_to(resolved_root):
        raise SandboxError(f"{what} escapes the sandbox workspace: {relpath!r}")
    return target


def contained_file(root: Path, relpath: str) -> Path | None:
    """:func:`contained_path`, as an **answer** rather than an exception, and only for a
    file that is actually there.

    Two callers need exactly this shape and neither is acting on a path the agent chose:
    the file picker, describing what the operator may reference, and the turn composer,
    resolving the references they sent. For both, a path that does not resolve is an
    ordinary outcome — the operator is typing, and half a list beats an error — while for
    every existing caller of ``contained_path`` an escape is a fault to raise on. Same
    check, different register; the check itself is not restated.

    A directory is refused too. ``@src`` is not a file reference, and letting one through
    would hand the model a path its read tool cannot open.
    """
    if not relpath or relpath.startswith("/") or "\0" in relpath:
        return None
    try:
        target = contained_path(root, relpath, what="file reference")
        return target if target.is_file() else None
    except (SandboxError, OSError):
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
    env: Mapping[str, str] = field(default_factory=dict)
    timeout_s: float = 30.0


@dataclass(frozen=True)
class SandboxResult:
    """What an isolated run produced. A non-zero ``exit_code`` or a ``timed_out``
    run is a normal result the agent reasons about — not an error."""

    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out
