"""What a path argument is measured against: one directory, or the agent's home."""

from __future__ import annotations

import posixpath
from dataclasses import dataclass
from pathlib import Path

from services.home.paths import HOME, SCRATCH, HomePathError, Place, parse, places_named
from services.sandbox.base import SandboxError, contained_path


@dataclass(frozen=True, slots=True)
class HomeFence:
    """The thread's home: where its command runs, the places it holds, and the ones it
    may write."""

    places: frozenset[Place]
    writable: frozenset[Place]
    cwd: str = f"{HOME}/scratch"

    def holds(self, raw: str, *, write: bool) -> bool:
        if not write and _job_private(raw):
            return True
        try:
            place = parse(raw, cwd=self.cwd).place
        except HomePathError:
            return False
        return place in (self.writable if write else self.places)

    def scratch_only(self, command: str) -> bool:
        """Whether ``command`` would see the thread's scratch and nothing else of the home:
        it stands in scratch and names no project or worktree."""
        try:
            here = parse(self.cwd).place
        except HomePathError:
            return False
        return here == SCRATCH and not places_named(command)


#: Folders that exist only inside the conversation's VM, never on the host, and the
#: streams every process has.
_JOB_PRIVATE = ("/tmp", "/dev/shm")
_STREAMS = frozenset({"/dev/null", "/dev/stdin", "/dev/stdout", "/dev/stderr"})


def _job_private(raw: str) -> bool:
    """Whether a path is the command's own: nothing outside it reads or keeps it."""
    text = raw.strip()
    if not text.startswith("/") or "\0" in text:
        return False
    normal = posixpath.normpath(text)
    if normal in _STREAMS:
        return True
    return any(normal == root or normal.startswith(f"{root}/") for root in _JOB_PRIVATE)


type Fence = Path | HomeFence


def escapes(fence: Fence | None, raw: str, *, write: bool = False) -> bool:
    """Whether a path argument could land outside ``fence``.

    Against a directory, ``~`` is the operator's own home, so a path naming it is refused
    outright rather than resolved. Against the agent's home, ``~`` is that home, and a path
    escapes when it names no place the thread holds; ``write`` narrows that to the places
    it may write. A shell command is measured with ``write`` off because the guest mounts
    an unwritable place read-only, which is the write fence for a command.

    With no fence, an absolute or upward path is treated as escaping: there is nothing to
    measure it against, and "we could not tell" has to read the same as "it left".
    """
    if isinstance(fence, HomeFence):
        return not fence.holds(raw, write=write)
    if raw.startswith("~"):
        return True
    path = Path(raw)
    if fence is None:
        return path.is_absolute() or ".." in path.parts
    try:
        _ = contained_path(fence, raw, what="path")
    except SandboxError:
        return True
    return False
