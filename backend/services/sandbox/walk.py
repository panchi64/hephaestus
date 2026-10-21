"""What a walk of a workspace counts as part of it.

One question, asked by every reader that lists a workspace: the history snapshot and the
per-turn workspace block the model reads, among them. A walk that kept a different set
from another's would have the block advertising a file the snapshot does not carry.

The exclusions here are **cosmetic only — nothing in this module deletes anything.** A
workspace's files stay on disk for as long as the conversation does; what these names
decide is which of them are worth *reporting*: a virtualenv and a `node_modules` are
thousands of files the agent did not write.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Iterator
from fnmatch import fnmatch
from pathlib import Path


def excluded(arcname: str, excludes: Iterable[str]) -> bool:
    """Whether a walk skips ``arcname``.

    Every *part* of the path is matched, not just its last one — so ``build`` skips
    ``packages/x/build/`` as readily as a top-level ``build/``. That breadth was a
    hazard while this list also decided what a reap threw away; now that a walk only
    decides what gets *reported*, it is simply the reading that keeps a nested
    virtualenv out of the block.
    """
    parts = Path(arcname).parts
    return any(fnmatch(part, pat) for part in parts for pat in excludes)


def walk_files(root: Path, excludes: Iterable[str]) -> Iterator[tuple[str, Path]]:
    """Every file a walk of ``root`` keeps, as ``(relpath, path)``, in order.

    Symlinks are skipped: the agent can plant one anywhere, and following it leaves
    the workspace.
    """
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not excluded(d, excludes))
        for name in sorted(filenames):
            full = Path(dirpath) / name
            if full.is_symlink():
                continue
            rel = full.relative_to(root).as_posix()
            if excluded(rel, excludes):
                continue
            yield rel, full
