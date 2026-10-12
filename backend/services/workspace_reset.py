"""Wiping a workspace whose database is gone but whose encryption key is not.

Deleting ``data/app.db`` does not reset anything: the thing that says a workspace
exists is ``data/keyfile.json`` — the Argon2 verifier and the wrapped DEK — and it
sits beside the database rather than inside it. An operator who clears the database
to start over is therefore still asked to unlock, with a key that now protects an
empty workspace.

This is the other half of that: remove the key **and everything sealed under it**, so
"start fresh" means what it says. It is deliberately a wipe of ``data_dir`` rather
than a list of the directories we happen to know about today — everything under there
is runtime state the app wrote for itself (uploads, the corpus and its index, sandbox
work and sealed archives, View snapshots, the browser profile), and a sealed directory
added next month must be caught by default instead of quietly surviving a reset and
outliving the only key that could read it.

Two entries are held back, and only two:

* **the live database file** (plus its ``-wal``/``-shm`` siblings) — SQLAlchemy holds
  it open, and it is empty anyway; that is the precondition for being here at all.
* **``searxng/``** — the managed SearXNG's persisted secret key, not operator data.

Coding-mode worktrees live outside ``data_dir`` on purpose (see ``Settings``), so a
reset never reaches the operator's own checkouts.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

KEYFILE_NAME = "keyfile.json"

#: Entries under ``data_dir`` a reset leaves alone. See the module docstring.
_PRESERVED = frozenset({"app.db", "app.db-wal", "app.db-shm", "searxng"})


@dataclass
class ResetSummary:
    """What the wipe actually removed — reported back so the operator is told the
    truth about their own disk rather than a hopeful message composed in advance."""

    removed: list[str] = field(default_factory=list)
    bytes_freed: int = 0
    failed: list[str] = field(default_factory=list)


def _size_of(path: Path) -> int:
    if path.is_file() or path.is_symlink():
        try:
            return path.lstat().st_size
        except OSError:
            return 0
    total = 0
    for child in path.rglob("*"):
        try:
            if child.is_file() or child.is_symlink():
                total += child.lstat().st_size
        except OSError:
            continue
    return total


def reset_workspace(data_dir: Path) -> ResetSummary:
    """Remove the keyfile and every other workspace artifact under ``data_dir``.

    Never raises for one stubborn entry: a file the OS refuses to unlink (an open
    handle on Windows, a permission the operator will have to fix by hand) is recorded
    in ``failed`` and the rest of the wipe continues. Stopping halfway would leave a
    workspace that is neither reset nor intact, and the caller can say what is left.
    """
    summary = ResetSummary()
    if not data_dir.is_dir():
        return summary

    # The keyfile goes first, and on its own: once it is gone the workspace reads as
    # uninitialized, so an interrupted wipe still lands the operator on setup rather
    # than back at a password prompt for a key that is half-buried.
    keyfile = data_dir / KEYFILE_NAME
    if keyfile.exists():
        size = _size_of(keyfile)
        try:
            keyfile.unlink()
        except OSError:
            logger.exception("workspace reset: could not remove the keyfile")
            summary.failed.append(KEYFILE_NAME)
        else:
            summary.removed.append(KEYFILE_NAME)
            summary.bytes_freed += size

    for entry in sorted(data_dir.iterdir()):
        if entry.name in _PRESERVED or entry.name == KEYFILE_NAME:
            continue
        size = _size_of(entry)
        try:
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry)
            else:
                entry.unlink()
        except OSError:
            logger.exception("workspace reset: could not remove %s", entry.name)
            summary.failed.append(entry.name)
        else:
            summary.removed.append(entry.name)
            summary.bytes_freed += size

    return summary
