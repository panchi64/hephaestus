"""A delegated agent's own copy of the workspace, and what comes back from it.

A child agent does not share its parent's workspace: sharing one means two agents
writing the same files with no record of who wrote what, and a parent that cannot tell
its own work from the work it asked for. So the child gets a **fork** — the parent's
whole workspace, copied — runs in its own guest copy behind its own confinement, and the parent
decides afterwards what to keep.

**The copy keeps everything the seal throws away.** ``.venv``, ``node_modules``,
``.local``: an archive drops them because they are rebuildable, but rebuilding them is
minutes the delegated agent would spend before its first useful command. Cloned
copy-on-write where the filesystem allows (:func:`core.fork.clone_tree`), so warmth is
close to free.

**The merge back is three-way, and it never overwrites.** A fork records the parent's
files as they stood the moment it was taken; a file the child changed lands only if the
parent's copy still matches that record. If both moved, the path is reported as a
conflict and the parent's version is left exactly as it is — a delegated agent's edit
silently clobbering work the parent did while it ran is the one outcome this whole shape
exists to prevent. Deletions are reported and never applied for the same reason: the
child dropping a file is not evidence the parent wanted it gone.

**A forked workspace is never sealed.** It is a copy of a workspace that is already
archived under its parent's key, so an archive of it would be a second at-rest copy of
the same files under a key no conversation will ever ask for again. The marker
(:func:`fork_marker`) is what tells the idle sweep that — it survives a crash, which is
the only moment anything else would find such a directory.
"""

from __future__ import annotations

import hashlib
import logging
import shutil
from collections.abc import Container, Iterable, Mapping
from pathlib import Path

from core.fork import MergeReport, clone_tree

from .base import SandboxError, contained_path
from .walk import walk_files

logger = logging.getLogger(__name__)


def fork_marker(workspace: Path) -> Path:
    """The flag that says: this workspace is a fork, so delete it rather than keep it.

    It is what makes a fork the one workspace a reap throws away, and what lets the
    idle sweep recognise one a crashed process stranded. A sibling of the directory
    rather than a file inside it: a file inside would be copied into the child's own
    forks and would land in the parent on a merge back.
    """
    return workspace.with_name(workspace.name + ".fork")


def clone_workspace(parent: Path, child: Path) -> None:
    """Copy a whole workspace onto the child's path — caches, virtualenvs and all.

    Blocking IO; call it off the event loop. The marker goes down *before* the
    directory, so a process that dies mid-copy leaves something the sweep recognizes as
    a fork to discard rather than a stranded workspace to seal.
    """
    marker = fork_marker(child)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.touch()
    # Whatever is on this path belongs to a fork that is already gone: a key names one
    # delegation, and a live child's session holds its own key against re-use.
    shutil.rmtree(child, ignore_errors=True)
    clone_tree(parent, child)


def manifest_of(root: Path, excludes: Iterable[str]) -> dict[str, str]:
    """``{relpath: sha256}`` of a workspace — what the merge back compares against.

    Hashes rather than mtimes: a copy-on-write clone and a restore both rewrite
    timestamps, and a merge that trusted them would call every file in the tree changed.
    """
    digests: dict[str, str] = {}
    for rel, path in walk_files(root, excludes):
        try:
            digests[rel] = _digest(path)
        except OSError:
            continue  # unreadable now ⇒ absent from the record; the merge treats it as new
    return digests


def merge_workspace(
    *, child: Path, parent: Path, manifest: Mapping[str, str], excludes: Iterable[str]
) -> MergeReport:
    """Land what the child changed back in the parent, and report what could not.

    Blocking IO; call it off the event loop. Three outcomes per path, decided against
    the fork-time ``manifest``: unchanged in the child (nothing to do), changed in the
    child only (copied), or changed in both (a conflict, left alone). A file the child
    deleted is reported and kept.

    Every destination goes through :func:`~services.sandbox.base.contained_path`, because
    this is the one writer in the fork path that runs on the *host*: a symlink planted in
    the parent's workspace — the agent in the box can make one point anywhere — would
    otherwise have ``copy2`` follow it and write the operator's own files, past every
    fence and past the gate a merge is supposed to be. A destination that leaves the
    workspace is reported like any other path that could not land.
    """
    files: list[str] = []
    conflicts: list[str] = []
    for rel, path in walk_files(child, excludes):
        base = manifest.get(rel)
        try:
            target = contained_path(parent, rel, what="merged file")
        except SandboxError:
            logger.warning("fork: %s does not land inside %s — refusing to merge it", rel, parent)
            conflicts.append(rel)
            continue
        try:
            if _digest(path) == base:
                continue  # the child never touched it
            landed = _digest(target) if target.is_file() else None
        except OSError:
            conflicts.append(rel)
            continue
        if landed != base:
            # The parent moved too (or never had this path). Its copy wins by default:
            # nothing here is allowed to overwrite work the operator's own thread did.
            conflicts.append(rel)
            continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
        except OSError:
            logger.warning("fork: could not merge %s back into %s", rel, parent)
            conflicts.append(rel)
            continue
        files.append(rel)
    deleted = [rel for rel in manifest if not (child / rel).exists()]
    return MergeReport(
        merged=not conflicts,
        files=sorted(files),
        conflicts=sorted(conflicts),
        deleted=sorted(deleted),
    )


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def orphan_fork_keys(work_root: Path, taken: Container[str]) -> list[str]:
    """The delegated forks under ``work_root`` that nothing owns any more.

    A fork is the one workspace directory a crash can strand that nobody will ever ask
    for again: its files copy a parent workspace that still exists under its own key,
    and its delegation ended when the process died. Every other directory here is an
    ordinary conversation's files, which is not a leftover and must never be collected.

    ``taken`` is whatever the caller still considers live — the session map and its
    in-flight teardowns. Passed in rather than reached for, so this stays a pure reading
    of a directory and the caller keeps the answer atomic against its own maps.
    """
    if not work_root.exists():
        return []
    return [
        path.name
        for path in sorted(work_root.iterdir())
        if path.is_dir()
        and path.name.startswith("s")  # `safe_key`'s prefix — never a scratch dir
        and path.name not in taken
        and fork_marker(path).exists()
    ]
