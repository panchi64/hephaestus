"""The git primitives every worktree operation is built out of.

One invocation, the identity the chassis commits under, the branch namespace, and the
commit that has to happen before anything *reads* a branch. Split out of
``worktree.py`` because forking (``fork.py``) needs exactly these and nothing else of
what the manager knows — and importing them back out of the manager's module would be a
cycle between the two halves of one layer.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

#: Branch namespace. One per worktree conversation, so a branch is self-describing and a
#: stale one is obvious.
BRANCH_PREFIX = "ody/"

#: The identity every commit this layer makes is attributed to. It is the chassis
#: committing on the agent's behalf, and it should read that way in `git log`.
AUTHOR = ("-c", "user.name=Odysseus", "-c", "user.email=odysseus@localhost")


class WorktreeError(Exception):
    """A git operation failed, with git's own message."""


@dataclass(frozen=True)
class WorktreeState:
    path: Path
    branch: str
    base_ref: str


def branch_for(conversation_id: str) -> str:
    return f"{BRANCH_PREFIX}{conversation_id}"


def child_branch_for(conversation_id: str, delegation_id: str) -> str:
    """The branch a delegated child works on, beside its parent's rather than under it.

    A dash and not a slash: git stores a branch as a file under ``refs/heads``, so
    ``ody/<conv>/<delegation>`` cannot exist while ``ody/<conv>`` does — and the parent's
    branch is the very thing this one is cut from.
    """
    return f"{branch_for(conversation_id)}-{delegation_id}"


async def run_git(cwd: Path, *args: str) -> tuple[int, str, str]:
    """One git invocation. Fixed argv, no shell — a project path is operator content and
    must never be word-split or interpolated into a command line.

    A missing `cwd` comes back as a failed git call rather than an `OSError`: the
    operator can move or delete a project directory at any time, and every caller here
    already knows how to handle "git said no" while none of them expects an exception
    from a path that existed a moment ago.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (OSError, NotADirectoryError) as exc:
        return 1, "", f"{cwd} is not reachable: {exc}"
    out, err = await proc.communicate()
    return (
        proc.returncode or 0,
        out.decode("utf-8", "replace"),
        err.decode("utf-8", "replace"),
    )


async def run_git_ok(cwd: Path, *args: str) -> str:
    code, out, err = await run_git(cwd, *args)
    if code != 0:
        raise WorktreeError((err or out).strip() or f"git {' '.join(args)} failed")
    return out


async def commit_worktree(path: Path, message: str | None = None) -> bool:
    """Stage and commit everything in ``path``, or return False if it was clean.

    Best-effort by design: a worktree that isn't there, or a git that refuses, must
    not take down the read that asked for this. `.odysseus/` ignores itself, so the
    agent's staged attachments and skill bundles never reach the operator's diff.
    """
    if not (path / ".git").exists():
        return False
    await run_git(path, "add", "-A")
    # `diff --cached --quiet` exits 0 when nothing is staged — the cheapest way to
    # ask "is there anything to commit" without parsing porcelain.
    clean, _, _ = await run_git(path, "diff", "--cached", "--quiet")
    if clean == 0:
        return False
    code, out, err = await run_git(path, *AUTHOR, "commit", "-m", message or "Agent changes")
    if code != 0:
        logger.warning("worktree: could not commit %s: %s", path, (err or out).strip())
        return False
    return True
