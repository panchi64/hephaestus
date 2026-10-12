"""The execution-sandbox capability — interface, value types, and errors.

Container-backed execution runs *through* this interface, isolated from the host.
The invariant it promises: the executed code sees only **copies** of the files
explicitly handed to it (``SandboxSpec.files``), cannot read or modify the host
filesystem / processes / environment, and reaches the network only through the
allowlisting proxy its workspace is fenced by (see
:mod:`services.sandbox.sidecar`) — never directly, and never at all on this
interface's own one-shot path. Outputs return explicitly (stdout/stderr +
copied-out files); nothing escapes the box as a side effect.

Not everything the agent runs arrives here, and saying otherwise would overstate
the invariant: a project thread's shell and the approved host command execute on
the operator's own machine under OS-level confinement
(:mod:`services.sandbox.host`), which fences the same domains and hides the
credential paths, but is a different boundary from this one.

Pluggable by design: :class:`Sandbox` is the seam, the default backend is a
container runtime (``container.ContainerSandbox``), and a fake is injected in
tests. The factory in ``detect.py`` **fails closed** — if no backend is available
the capability is *absent* (``None``), never a silent host fallback.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from core.exceptions import OdysseusError

_SAFE = re.compile(r"[^A-Za-z0-9_.-]")


def safe_key(key: str) -> str:
    """A container/dir-safe token for a workspace key (leading char guaranteed).

    Here rather than beside the session manager because the key names more than a
    container: the egress policy derives the allowlist directory a fence bind-mounts from
    the same key, and a second spelling of "safe" would file a workspace's allowlist
    under a different name from the workspace.
    """
    return "s" + _SAFE.sub("-", key)


class SandboxError(OdysseusError):
    """The sandbox backend failed to run the request (not a non-zero exit — that
    is an ordinary :class:`SandboxResult` the agent acts on). This is an
    infrastructure failure: the runtime vanished, the image is missing, the
    container could not start."""


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
class SandboxFile:
    """A file copied *into* the box before execution — a copy of an operator
    file, never the original. ``path`` is relative to the working directory."""

    path: str
    content: bytes


@dataclass(frozen=True)
class SandboxSpec:
    """One isolated execution request.

    ``command`` is an argv run inside the box's working directory (e.g.
    ``["python", "-c", code]`` or ``["bash", "-c", script]``). ``files`` are
    copied in; ``outputs`` names files to copy back out after the run. ``env`` is
    the *only* environment the process sees — the host environment never leaks in.
    """

    command: Sequence[str]
    files: Sequence[SandboxFile] = ()
    outputs: Sequence[str] = ()
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
    outputs: Mapping[str, bytes] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


class Sandbox(ABC):
    """A host-isolated execution backend. Implementations honor the invariant in
    the module docstring; the contract is just :meth:`run` over a spec."""

    #: A short, stable name for the backend (surfaced to the operator/logs).
    name: str = "sandbox"

    @abstractmethod
    async def available(self) -> bool:
        """Whether this backend can actually run right now (the runtime is
        present and responsive). The factory uses it to fail closed."""

    @abstractmethod
    async def run(self, spec: SandboxSpec) -> SandboxResult:
        """Execute ``spec`` in isolation and return its result. Raises
        :class:`SandboxError` only on infrastructure failure."""
