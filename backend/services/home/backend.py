"""The file side of the agent's home, as a Pydantic AI workspace backend.

Every place the home holds is a host directory (a mount). A virtual path is read by
``paths.parse`` and its file operation runs on that mount's directory through Pydantic AI's
own local backend, so the semantics are the library's. **This class is the containment
boundary**: each operation's real target, symlinks resolved, must stay inside its mount's
directory, and a write needs a writable mount.

The home's own structure (``~``, ``~/projects``, …) is virtual: it can be listed and
stat'ed, and refuses reads and writes like a directory the agent does not own.
"""

from __future__ import annotations

import os
import posixpath
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from pydantic_ai.workspaces import FileEntry, LocalWorkspaceBackend, WorkspaceRef
from pydantic_ai.workspaces.protocol import WorkspaceReadOnlyError

from .paths import HOME, HomePath, HomePathError, Place, absolute, parse

#: The group folders under the home, in the order a listing shows them.
_GROUPS = ("scratch", "projects", "worktrees", "shared")


@dataclass(frozen=True, slots=True)
class Mount:
    """One place in the home, on its host directory."""

    place: Place
    root: Path
    writable: bool


class HomeBackend:
    """Implements ``WorkspaceBackend``, ``SupportsFilesystem`` and ``SupportsRealpath``."""

    def __init__(self, mounts: Iterable[Mount], *, key: str, working_dir: str = HOME) -> None:
        self._mounts: dict[Place, Mount] = {mount.place: mount for mount in mounts}
        self._locals: dict[Place, LocalWorkspaceBackend] = {
            place: LocalWorkspaceBackend(mount.root) for place, mount in self._mounts.items()
        }
        self._ref: WorkspaceRef = WorkspaceRef(provider="hephaestus-home", id=key)
        self._working_dir: str = working_dir

    @property
    def ref(self) -> WorkspaceRef:
        return self._ref

    @property
    def mounts(self) -> Sequence[Mount]:
        return list(self._mounts.values())

    async def working_dir(self) -> str:
        return self._working_dir

    def _spell(self, path: str) -> str:
        return absolute(path, cwd=self._working_dir)

    def _virtual_children(self, path: str) -> list[str] | None:
        """The names in a virtual directory of the home, or None when ``path`` is not one."""
        if path == HOME:
            return list(_GROUPS)
        group = path.removeprefix(f"{HOME}/")
        if path.startswith(f"{HOME}/") and group in _GROUPS and group != "scratch":
            return sorted(p.name for p in self._mounts if p.kind == group)
        return None

    def _host(self, path: str) -> tuple[Mount, Path, HomePath]:
        """The mount and host path a virtual path names, refusing one outside its mount."""
        target = parse(path, cwd=self._working_dir)
        mount = self._mounts.get(target.place)
        if mount is None:
            raise FileNotFoundError(f"there is no {target.place.path} in your home")
        root = mount.root.resolve()
        host = root / target.rel if target.rel else root
        real = Path(os.path.realpath(host))
        if not real.is_relative_to(root):
            raise PermissionError(f"{path!r} leads outside {target.place.path}")
        return mount, host, target

    def _writable(self, path: str) -> tuple[Mount, Path, HomePath]:
        mount, host, target = self._host(path)
        if not mount.writable:
            raise WorkspaceReadOnlyError(
                f"{mount.place.path} is read-only to you here; writing to it needs the "
                + "operator's approval"
            )
        return mount, host, target

    def _local(self, mount: Mount) -> LocalWorkspaceBackend:
        return self._locals[mount.place]

    # --- SupportsFilesystem -------------------------------------------------------------

    async def read_bytes(self, path: str) -> bytes:
        if self._virtual_children(self._spell(path)) is not None:
            raise IsADirectoryError(path)
        mount, host, _ = self._host(path)
        return await self._local(mount).read_bytes(str(host))

    async def write_bytes(self, path: str, data: bytes) -> None:
        if self._virtual_children(self._spell(path)) is not None:
            raise IsADirectoryError(path)
        mount, host, target = self._writable(path)
        if not target.rel:
            raise IsADirectoryError(path)
        await self._local(mount).write_bytes(str(host), data)

    async def stat(self, path: str) -> FileEntry:
        normal = self._spell(path)
        if self._virtual_children(normal) is not None:
            return FileEntry(name=posixpath.basename(normal), path=normal, is_dir=True, size=None)
        mount, host, target = self._host(path)
        entry = await self._local(mount).stat(str(host))
        return FileEntry(name=entry.name, path=target.path, is_dir=entry.is_dir, size=entry.size)

    async def list_dir(self, path: str) -> Sequence[FileEntry]:
        normal = self._spell(path)
        children = self._virtual_children(normal)
        if children is not None:
            return [
                FileEntry(name=name, path=f"{normal}/{name}", is_dir=True, size=None)
                for name in children
            ]
        mount, host, target = self._host(path)
        entries = await self._local(mount).list_dir(str(host))
        return [
            FileEntry(
                name=entry.name,
                path=f"{target.path}/{entry.name}",
                is_dir=entry.is_dir,
                size=entry.size,
            )
            for entry in entries
        ]

    async def make_dir(self, path: str) -> None:
        if self._virtual_children(self._spell(path)) is not None:
            return
        mount, host, _ = self._writable(path)
        await self._local(mount).make_dir(str(host))

    async def remove(self, path: str) -> None:
        normal = self._spell(path)
        above_home = normal == "/" or HOME.startswith(normal.rstrip("/") + "/")
        if above_home or self._virtual_children(normal) is not None:
            raise ValueError("cannot remove a folder of the home itself")
        mount, host, target = self._writable(path)
        if not target.rel:
            raise ValueError(f"cannot remove {target.place.path} itself")
        await self._local(mount).remove(str(host))

    async def exists(self, path: str) -> bool:
        if self._virtual_children(self._spell(path)) is not None:
            return True
        try:
            mount, host, _ = self._host(path)
        except HomePathError, FileNotFoundError, PermissionError:
            return False
        return await self._local(mount).exists(str(host))

    # --- SupportsRealpath ---------------------------------------------------------------

    async def realpath(self, path: str) -> str:
        """The virtual path with its symlinks followed, refusing one that leads out of its
        place (``OSError``, per the protocol)."""
        normal = self._spell(path)
        if self._virtual_children(normal) is not None:
            return normal
        try:
            mount, host, target = self._host(path)
        except (HomePathError, FileNotFoundError, PermissionError) as exc:
            raise OSError(str(exc)) from exc
        root = mount.root.resolve()
        real = Path(os.path.realpath(host))
        rel = real.relative_to(root).as_posix()
        return target.place.path if rel == "." else f"{target.place.path}/{rel}"
