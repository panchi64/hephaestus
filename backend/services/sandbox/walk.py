"""What a walk of a workspace counts as part of it.

Every reader that lists a workspace (the history snapshot, the per-turn block the model
reads) shares this one answer, so the block never advertises a file the snapshot lacks.

The exclusions are cosmetic: nothing here deletes anything. They decide which files are
worth reporting; a virtualenv or `node_modules` is thousands the agent did not write.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Iterator
from fnmatch import fnmatch
from pathlib import Path


def excluded(arcname: str, excludes: Iterable[str]) -> bool:
    """Whether a walk skips ``arcname``.

    Every part of the path is matched, so ``build`` skips ``packages/x/build/`` as well as a
    top-level ``build/``, keeping nested virtualenvs out of the block.
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
