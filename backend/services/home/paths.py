"""Where a path the model wrote lands in the agent's home.

The home is one fixed layout, the same for every thread::

    ~/scratch                 this thread's own scratch space (also ``/work``)
    ~/projects/<slug>         each project the operator registered
    ~/worktrees/<name>        a worktree the agent opened on a project
    ~/shared/<name>           a folder that outlives threads

``~`` is ``/home/agent``. A path is read as text only, never against a filesystem: this
module says which place a path names and what it names inside it, and refuses anything
that climbs out of a place or names the home's own structure as if it were a file.
Resolving that onto a host directory, symlinks and all, is the mount table's job.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass
from typing import Literal

from core.exceptions import InvalidInputError

HOME = "/home/agent"
SCRATCH_ALIAS = "/work"

type PlaceKind = Literal["scratch", "projects", "worktrees", "shared"]
_NAMED: dict[str, PlaceKind] = {
    "projects": "projects",
    "worktrees": "worktrees",
    "shared": "shared",
}
_LAYOUT = "a path under ~/scratch, ~/projects/<name>, ~/worktrees/<name> or ~/shared/<name>"
#: A named place's name: one lowercase path component, safe as a VM mount target.
_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,79}$")
_NAMED_PLACE = re.compile(r"(?:~|/home/agent)/(projects|worktrees)/([a-z0-9][a-z0-9._-]{0,79})")


def places_named(text: str) -> list[Place]:
    """The projects and worktrees a command line names by home path, in order, each once."""
    places: list[Place] = []
    for match in _NAMED_PLACE.finditer(text):
        group: str = match.group(1)
        name: str = match.group(2)
        place = Place("worktrees" if group == "worktrees" else "projects", name)
        if place not in places:
            places.append(place)
    return places


@dataclass(frozen=True, slots=True)
class Place:
    """One folder in the home: scratch, or a named project, worktree or shared folder."""

    kind: PlaceKind
    name: str = ""

    @property
    def path(self) -> str:
        """The place as the model sees it, absolute."""
        return f"{HOME}/scratch" if self.kind == "scratch" else f"{HOME}/{self.kind}/{self.name}"


SCRATCH = Place("scratch")


@dataclass(frozen=True, slots=True)
class HomePath:
    """A path inside one place: ``rel`` is relative to the place's root, ``""`` for the
    root itself, and never climbs above it."""

    place: Place
    rel: str = ""

    @property
    def path(self) -> str:
        return self.place.path if not self.rel else f"{self.place.path}/{self.rel}"


class HomePathError(InvalidInputError):
    """A path that names nothing inside a place, worded for the model."""


def _absolute(raw: str, cwd: str) -> str:
    """``raw`` as an absolute path in the home's terms, before normalisation."""
    text = raw.strip()
    if not text or "\0" in text:
        raise HomePathError("empty path", hint="name a file or folder under ~")
    if text == "~" or text.startswith("~/"):
        return HOME + text[1:]
    if text == SCRATCH_ALIAS or text.startswith(SCRATCH_ALIAS + "/"):
        return f"{HOME}/scratch" + text[len(SCRATCH_ALIAS) :]
    if text.startswith("/"):
        return text
    return f"{cwd.rstrip('/')}/{text}"


def climbs_out(raw: str, *, cwd: str) -> HomePathError | None:
    """Why ``raw`` is refused when its ``..`` leaves the place it starts in, else None. A
    climb out and back in is still a climb out."""
    started = _place_named_by(_absolute(raw, cwd))
    if started is None:
        return None
    normal = absolute(raw, cwd=cwd)
    if normal == started.path or normal.startswith(f"{started.path}/"):
        return None
    try:
        landed = _place_named_by(normal)
    except HomePathError:
        landed = None
    return HomePathError(
        f"{raw!r} climbs out of {started.path}",
        hint=f"name the other folder directly, as {landed.path}"
        if landed is not None
        else f"name {_LAYOUT}",
    )


def absolute(raw: str, *, cwd: str = f"{HOME}/scratch") -> str:
    """``raw`` as a normalised absolute path in the home's spelling, checked for nothing
    beyond being a path: ``~`` and ``/work`` expanded, a relative path read from ``cwd``."""
    return posixpath.normpath(_absolute(raw, cwd))


def parse(raw: str, *, cwd: str = f"{HOME}/scratch") -> HomePath:
    """The place and in-place path ``raw`` names, read from ``cwd`` when relative.

    Refuses a path outside the home, one naming the home or a group folder itself
    (``~``, ``~/projects``), and one whose ``..`` climbs out of the place it starts in.
    """
    normal = absolute(raw, cwd=cwd)
    if normal != HOME and not normal.startswith(HOME + "/"):
        raise HomePathError(f"{raw!r} is outside your home", hint=f"name {_LAYOUT}")
    parts = normal[len(HOME) + 1 :].split("/") if normal != HOME else []
    place = _place_of(raw, parts)
    depth = 1 if place.kind == "scratch" else 2
    rel = "/".join(parts[depth:])
    climb = climbs_out(raw, cwd=cwd)
    if climb is not None:
        raise climb
    return HomePath(place, rel)


def _place_of(raw: str, parts: list[str]) -> Place:
    if not parts:
        raise HomePathError(
            f"{raw!r} is your home itself, not a folder in it", hint=f"name {_LAYOUT}"
        )
    head = parts[0]
    if head == "scratch":
        return SCRATCH
    kind = _NAMED.get(head)
    if kind is not None:
        if len(parts) < 2 or not parts[1]:
            raise HomePathError(
                f"{raw!r} is the {head} folder itself",
                hint=f"name one of them, as ~/{head}/<name>",
            )
        if not _NAME.match(parts[1]):
            raise HomePathError(
                f"there is no {head} folder named {parts[1]!r}",
                hint=f"list ~/{head} to see the names",
            )
        return Place(kind, parts[1])
    raise HomePathError(
        f"{raw!r} is not in a folder your home has",
        hint="your home holds ~/scratch, ~/projects, ~/worktrees and ~/shared",
    )


def _place_named_by(absolute: str) -> Place | None:
    """The place the leading segments of an unnormalised path name, if they name one
    before any ``..``."""
    if not absolute.startswith(HOME + "/"):
        return None
    parts = absolute[len(HOME) + 1 :].split("/")
    if not parts or parts[0] in ("", "..", "."):
        return None
    if parts[0] == "scratch":
        return SCRATCH
    if parts[0] in _NAMED and len(parts) > 1 and parts[1] not in ("", "..", "."):
        return _place_of(absolute, parts[:2])
    return None
