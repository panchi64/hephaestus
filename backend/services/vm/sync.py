"""Moving a workspace across the VM boundary, from the host's side.

The host copy is the workspace; the guest copy is a working set that exists only while
the workspace is in use. Both sides are compared by manifest (path → kind, size, mtime),
and only what changed crosses, as a tar archive.

Everything coming out of the guest is untrusted. :func:`unpack` accepts only regular
files, directories and symlinks, at relative paths that stay inside the workspace and
never pass through a symlink, under a byte budget. A symlink is stored as one and never
followed, like any symlink the agent leaves in its workspace.
"""

from __future__ import annotations

import os
import shutil
import stat
import tarfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import IO

from pydantic import JsonValue

from .driver import VMError

#: path → (kind, size, mtime_ns); kind is ``f`` file, ``d`` directory or ``l`` symlink.
type Manifest = dict[str, tuple[str, int, int]]


@dataclass(frozen=True)
class Changes:
    changed: list[str]
    gone: list[str]

    def __bool__(self) -> bool:
        return bool(self.changed or self.gone)


def scan(root: Path) -> Manifest:
    entries: Manifest = {}
    if not root.is_dir():
        return entries
    for dirpath, dirnames, filenames in os.walk(root):
        for name in [*dirnames, *filenames]:
            full = os.path.join(dirpath, name)
            info = os.lstat(full)
            rel = os.path.relpath(full, root)
            if stat.S_ISLNK(info.st_mode):
                entries[rel] = ("l", 0, info.st_mtime_ns)
            elif stat.S_ISDIR(info.st_mode):
                entries[rel] = ("d", 0, 0)
            elif stat.S_ISREG(info.st_mode):
                entries[rel] = ("f", info.st_size, info.st_mtime_ns)
    return entries


def scan_names(root: Path, names: set[str]) -> Manifest:
    """:func:`scan`, for only ``names`` — without walking what they leave out."""
    entries: Manifest = {}
    for name in names:
        try:
            info = os.lstat(root / name)
        except OSError:
            continue
        if stat.S_ISLNK(info.st_mode):
            entries[name] = ("l", 0, info.st_mtime_ns)
        elif stat.S_ISDIR(info.st_mode):
            entries[name] = ("d", 0, 0)
        elif stat.S_ISREG(info.st_mode):
            entries[name] = ("f", info.st_size, info.st_mtime_ns)
    return entries


def as_int(value: JsonValue) -> int:
    """``int(value)``; raises ``TypeError`` for null, arrays and objects."""
    if isinstance(value, bool | int | float | str):
        return int(value)
    raise TypeError(f"not a number: {value!r}")


def parse_manifest(raw: dict[str, JsonValue]) -> Manifest:
    return {
        str(name): (str(entry[0]), as_int(entry[1]), as_int(entry[2]))
        for name, entry in raw.items()
        if isinstance(entry, list) and len(entry) == 3
    }


def diff(now: Manifest, before: Manifest) -> Changes:
    """What changed from ``before`` to ``now``. A path whose kind changed is both gone
    and changed, so the old one is removed before the new one lands."""
    changed = [name for name, entry in now.items() if before.get(name) != entry]
    gone = [name for name, entry in before.items() if name not in now or now[name][0] != entry[0]]
    return Changes(changed=sorted(changed), gone=sorted(gone))


def pack(root: Path, names: list[str], out: Path) -> None:
    with tarfile.open(out, "w", format=tarfile.PAX_FORMAT) as archive:
        for name in names:
            full = root / name
            if not os.path.lexists(full):
                continue
            info = archive.gettarinfo(str(full), arcname=name)
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            if info.isreg():
                with full.open("rb") as handle:
                    archive.addfile(info, handle)
            elif info.isdir() or info.issym():
                archive.addfile(info)


def _relative(name: str, *, project: bool = False) -> PurePosixPath:
    path = PurePosixPath(name)
    if path.is_absolute() or not path.parts or ".." in path.parts or "\0" in name:
        raise VMError(f"the VM returned a path outside the workspace: {name!r}")
    if project and path.parts[0] == ".git":
        raise VMError(f"the VM returned a path inside .git: {name!r}")
    return path


def _link_escapes(rel: PurePosixPath, target: str) -> bool:
    if target.startswith("/"):
        return True
    depth = len(rel.parts) - 1
    for part in PurePosixPath(target).parts:
        depth += -1 if part == ".." else 0 if part == "." else 1
        if depth < 0:
            return True
    return False


def _through_symlink(root: Path, rel: PurePosixPath) -> bool:
    current = root
    for part in rel.parts[:-1]:
        current = current / part
        if current.is_symlink():
            return True
    return False


def _remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path, ignore_errors=True)
    elif os.path.lexists(path):
        path.unlink()


def remove_gone(root: Path, gone: list[str], *, project: bool = False) -> None:
    for name in sorted(gone, key=len, reverse=True):
        rel = _relative(name, project=project)
        if _through_symlink(root, rel):
            continue
        _remove(root / rel)


def unpack(
    root: Path, archive_path: Path, gone: list[str], *, max_bytes: int, project: bool = False
) -> None:
    """Apply an archive the guest produced, after removing what it no longer has.

    ``project`` is for an operator's project folder, where nothing may land in `.git/`
    and a symlink may not point outside the tree."""
    root.mkdir(parents=True, exist_ok=True)
    remove_gone(root, gone, project=project)
    budget = max_bytes
    with tarfile.open(archive_path) as archive:
        for member in archive:
            rel = _relative(member.name, project=project)
            if project and member.issym() and _link_escapes(rel, member.linkname):
                raise VMError(f"the VM returned a symlink out of the project: {member.name!r}")
            if _through_symlink(root, rel):
                raise VMError(f"the VM returned a path through a symlink: {member.name!r}")
            target = root / rel
            if member.isdir():
                if os.path.lexists(target) and not target.is_dir():
                    _remove(target)
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not (member.isreg() or member.issym()):
                raise VMError(f"the VM returned something that is not a file: {member.name!r}")
            budget -= member.size
            if budget < 0:
                raise VMError("the VM returned more than the workspace transfer limit")
            target.parent.mkdir(parents=True, exist_ok=True)
            if os.path.lexists(target):
                _remove(target)
            if member.issym():
                os.symlink(member.linkname, target)
                continue
            source = archive.extractfile(member)
            if source is None:
                continue
            _write(target, source, executable=bool(member.mode & 0o100))
            stamp = int(member.mtime * 1_000_000_000)
            os.utime(target, ns=(stamp, stamp))


def _write(target: Path, source: IO[bytes], *, executable: bool) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(target, flags, 0o755 if executable else 0o644)
    with os.fdopen(fd, "wb") as handle:
        shutil.copyfileobj(source, handle)
