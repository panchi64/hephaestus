"""What a path argument is measured against: one directory, or the agent's home."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from services.home.paths import HOME, HomePathError, Place, parse
from services.sandbox.base import SandboxError, contained_path


@dataclass(frozen=True, slots=True)
class HomeFence:
    """The thread's home: where its command runs, the places it holds, and the ones it
    may write."""

    places: frozenset[Place]
    writable: frozenset[Place]
    cwd: str = f"{HOME}/scratch"

    def holds(self, raw: str, *, write: bool) -> bool:
        try:
            place = parse(raw, cwd=self.cwd).place
        except HomePathError:
            return False
        return place in (self.writable if write else self.places)


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
