"""Which places a thread's home holds, on which folders, and which it may write."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

from services.permissions.fence import HomeFence
from services.projects.store import ProjectStore

from .backend import HomeBackend, Mount
from .leases import WriteLeases, place_key
from .paths import SCRATCH, HomePathError, Place, parse


@dataclass(frozen=True)
class RunHome:
    """One run's home: its mounts, the place a path naming no group lands in, and where
    its shell stands, which is what the judge reads a command's relative paths from."""

    mounts: tuple[Mount, ...]
    default: Place
    key: str
    cwd: str = ""
    backend: HomeBackend = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "backend", HomeBackend(self.mounts, key=self.key, default=self.default)
        )

    def mount(self, place: Place) -> Mount | None:
        return next((m for m in self.mounts if m.place == place), None)

    def place_of(self, path: str) -> Place | None:
        """The mounted place a file tool's path lands in, or None when it names none."""
        try:
            place = parse(self.backend.spell(path)).place
        except HomePathError:
            return None
        return place if self.mount(place) is not None else None

    def writable(self, place: Place) -> bool:
        mount = self.mount(place)
        return mount is not None and mount.writable

    def with_writable(self, place: Place) -> RunHome:
        mounts = tuple(replace(m, writable=True) if m.place == place else m for m in self.mounts)
        return replace(self, mounts=mounts)

    def fence(self, cwd: str | None = None) -> HomeFence:
        """The home as the permission judge measures a path against it."""
        return HomeFence(
            places=frozenset(m.place for m in self.mounts),
            writable=frozenset(m.place for m in self.mounts if m.writable),
            cwd=cwd or self.cwd or self.default.path,
        )


async def thread_mounts(
    *,
    owner_id: str,
    conversation_id: str | None,
    scratch: Path | None,
    projects: ProjectStore | None,
    leases: WriteLeases | None,
    filed_project_id: str | None = None,
    filed_root: Path | None = None,
) -> list[Mount]:
    """The thread's scratch, writable, then every registered project.

    A project is writable to a thread that holds a lease on it, or that was started in it:
    starting a thread in a project is the operator saying the agent works there, which is
    what a project thread has always meant. ``filed_root`` stands in for the filed
    project's own folder, for a thread that works in a worktree of it.
    """
    mounts = [] if scratch is None else [Mount(SCRATCH, scratch, writable=True)]
    if projects is None:
        return mounts
    slugs = await projects.slugs(owner_id)
    roots = await projects.workspace_roots(owner_id)
    held = (
        await leases.held(owner_id, conversation_id)
        if leases is not None and conversation_id
        else set[str]()
    )
    for project_id, slug in sorted(slugs.items(), key=lambda item: item[1]):
        root = roots.get(project_id)
        if root is None or not slug:
            continue
        place = Place("projects", slug)
        filed = project_id == filed_project_id
        writable = filed or place_key(place) in held
        mounts.append(Mount(place, filed_root if filed and filed_root else root, writable))
    return mounts


def default_place(mounts: Sequence[Mount], filed: Place | None) -> Place | None:
    """Where a path naming no group lands: the thread's project, else its scratch."""
    places = {m.place for m in mounts}
    if filed is not None and filed in places:
        return filed
    return SCRATCH if SCRATCH in places else None
