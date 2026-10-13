"""Worktree branches named before the project was renamed, moved into today's namespace.

Branch names are derived from the conversation id (``repo.branch_for``), so a thread
whose branch still carries the old prefix would lose track of its work. ``git branch -m``
also repoints a worktree that has the branch checked out.
"""

from __future__ import annotations

import logging
from pathlib import Path

from .repo import BRANCH_PREFIX, run_git

logger = logging.getLogger(__name__)

LEGACY_PREFIX = "ody/"


async def rename_legacy_branches(root: Path) -> list[str]:
    """Rename every ``ody/…`` branch in ``root`` to ``heph/…``. Returns the new names.
    Never raises: a branch that cannot move is logged and left where it is."""
    code, out, _ = await run_git(
        root, "for-each-ref", "--format=%(refname:short)", f"refs/heads/{LEGACY_PREFIX}"
    )
    if code != 0:
        return []
    renamed: list[str] = []
    for old in out.split():
        new = BRANCH_PREFIX + old.removeprefix(LEGACY_PREFIX)
        code, _, err = await run_git(root, "branch", "-m", old, new)
        if code == 0:
            renamed.append(new)
        else:
            logger.warning("projects: could not rename %s in %s: %s", old, root, err.strip())
    return renamed
