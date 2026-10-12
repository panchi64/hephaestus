"""Moves a workspace's files across the VM boundary, from the guest's side.

``manifest`` lists what a workspace holds; ``pack`` archives the paths the host asks for;
``unpack`` applies what the host sends. The host decides what moves, and checks every
archive it receives (``services/vm/sync.py``); this side only does the IO.

Standard library only; it is copied into the guest at boot.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import sys
import tarfile


def _entry(path: str) -> list | None:
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode):
        return ["l", 0, info.st_mtime_ns]
    if stat.S_ISDIR(info.st_mode):
        return ["d", 0, 0]
    if stat.S_ISREG(info.st_mode):
        return ["f", info.st_size, info.st_mtime_ns]
    return None


def manifest(root: str) -> int:
    entries: dict[str, list] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in [*dirnames, *filenames]:
            full = os.path.join(dirpath, name)
            entry = _entry(full)
            if entry is not None:
                entries[os.path.relpath(full, root)] = entry
    json.dump(entries, sys.stdout, separators=(",", ":"))
    return 0


def pack(root: str, listing: str, out: str) -> int:
    with open(listing, encoding="utf-8") as handle:
        names = json.load(handle)
    with tarfile.open(out, "w", format=tarfile.PAX_FORMAT) as archive:
        for name in names:
            full = os.path.join(root, name)
            if os.path.lexists(full):
                archive.add(full, arcname=name, recursive=False)
    return 0


def _remove(path: str) -> None:
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path, ignore_errors=True)
    elif os.path.lexists(path):
        os.unlink(path)


def unpack(root: str, archive_path: str, listing: str, uid: str) -> int:
    with open(listing, encoding="utf-8") as handle:
        gone = json.load(handle)
    for name in sorted(gone, key=len, reverse=True):
        _remove(os.path.join(root, name))
    owner = int(uid)
    os.makedirs(root, exist_ok=True)
    os.chown(root, owner, owner)
    with tarfile.open(archive_path) as archive:
        members = archive.getmembers()
        for member in members:
            target = os.path.join(root, member.name)
            if os.path.lexists(target) and not (member.isdir() and os.path.isdir(target)):
                _remove(target)
        archive.extractall(root, filter="tar")
    for member in members:
        for parent in _parents(member.name):
            os.lchown(os.path.join(root, parent), owner, owner)
    return 0


def _parents(name: str) -> list[str]:
    parts = name.split("/")
    return ["/".join(parts[: i + 1]) for i in range(len(parts))]


def wipe(root: str) -> int:
    shutil.rmtree(root, ignore_errors=True)
    return 0


if __name__ == "__main__":
    action, *rest = sys.argv[1:]
    handlers = {"manifest": manifest, "pack": pack, "unpack": unpack, "wipe": wipe}
    sys.exit(handlers[action](*rest))
