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
    """One run's home: its mounts, and the place a path naming no group lands in."""

    mounts: tuple[Mount, ...]
    default: Place
    key: str
    backend: HomeBackend = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "backend", HomeBackend(self.mounts, key=self.key, default=self.default)
        )

    def mount(self, place: Place) -> Mount | None:
        return next((m for m in self.mounts if m.place == place), None)

    def place_of(self, path: str) -> Place | None:
        """The mounted place a file tool's path lands in, or None when it names none."""
        located = self.locate(path)
        return located[0].place if located is not None else None

    def locate(self, path: str) -> tuple[Mount, str] | None:
        """The mount a file tool's path lands in and the path inside it, or None."""
        try:
            target = parse(self.backend.spell(path))
        except HomePathError:
            return None
        mount = self.mount(target.place)
        return (mount, target.rel) if mount is not None else None

    def writable(self, place: Place) -> bool:
        mount = self.mount(place)
        return mount is not None and mount.writable

    def with_writable(self, place: Place) -> RunHome:
        mounts = tuple(replace(m, writable=True) if m.place == place else m for m in self.mounts)
        return replace(self, mounts=mounts)

    def fence(self, cwd: str | None = None) -> HomeFence:
        """The home as the permission judge measures a path against it, from where the
        thread's shell stands (its default place before its first command)."""
        return HomeFence(
            places=frozenset(m.place for m in self.mounts),
            writable=frozenset(m.place for m in self.mounts if m.writable),
            cwd=cwd or self.default.path,
        )


async def thread_mounts(
    *,
    owner_id: str,
    scratch: Path | None,
    projects: ProjectStore | None,
    leases: WriteLeases | None,
    lease_holder: str | None,
    filed_project_id: str | None = None,
    filed_writable: bool = False,
    worktree_root: Path | None = None,
) -> list[Mount]:
    """The thread's scratch, writable, then every registered project, and the thread's
    worktree of its filed project at ``~/worktrees/<slug>``.

    A project is writable to a thread whose ``lease_holder`` holds a lease on it, and the
    filed project when ``filed_writable`` says so: starting a thread in a project is the
    operator saying the agent works there. The worktree is the thread's own branch, so it
    is always writable.
    """
    mounts = [] if scratch is None else [Mount(SCRATCH, scratch, writable=True)]
    if projects is None:
        return mounts
    slugs = await projects.slugs(owner_id)
    roots = await projects.workspace_roots(owner_id)
    held = (
        await leases.held(owner_id, lease_holder)
        if leases is not None and lease_holder
        else set[str]()
    )
    for project_id, slug in sorted(slugs.items(), key=lambda item: item[1]):
        root = roots.get(project_id)
        if root is None or not slug:
            continue
        place = Place("projects", slug)
        filed = project_id == filed_project_id
        writable = (filed and filed_writable) or place_key(place) in held
        mounts.append(Mount(place, root, writable, project_id=project_id))
        if filed and worktree_root is not None:
            mounts.append(
                Mount(Place("worktrees", slug), worktree_root, writable=True, project_id=project_id)
            )
    return mounts


def default_place(mounts: Sequence[Mount], filed_slug: str | None) -> Place | None:
    """Where a path naming no group lands: the thread's worktree, else the project it is
    filed under, else its scratch."""
    places = {m.place for m in mounts}
    if filed_slug:
        for place in (Place("worktrees", filed_slug), Place("projects", filed_slug)):
            if place in places:
                return place
    return SCRATCH if SCRATCH in places else None
