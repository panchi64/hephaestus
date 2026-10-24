"""The git primitives every worktree operation is built out of.

One invocation, the commit identity, the branch namespace, and the commit that must
happen before anything reads a branch. Split from ``worktree.py`` so forking
(``fork.py``) can import them without a cycle.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from services.sandbox.gitenv import GIT_ARG_PINS, git_config_pins

logger = logging.getLogger(__name__)

#: Branch namespace, one branch per worktree conversation.
BRANCH_PREFIX = "heph/"

#: The identity of every commit this layer makes: the chassis committing for the agent.
AUTHOR = ("-c", "user.name=Hephaestus", "-c", "user.email=hephaestus@localhost")


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

    A dash, not a slash: git stores a branch as a file under ``refs/heads``, so
    ``heph/<conv>/<delegation>`` cannot coexist with the parent's ``heph/<conv>``.
    """
    return f"{branch_for(conversation_id)}-{delegation_id}"


async def run_git(cwd: Path, *args: str) -> tuple[int, str, str]:
    """One git invocation with a fixed argv and no shell, so a project path is never
    word-split or interpolated.

    A missing `cwd` returns a failed call rather than raising `OSError`, since the operator
    can move a project directory at any time and callers handle "git said no".
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            *GIT_ARG_PINS,
            *args,
            cwd=str(cwd),
            env=git_config_pins(os.environ),
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
    not take down the read that asked for this. `.hephaestus/` ignores itself, so the
    agent's staged attachments and skill bundles never reach the operator's diff.
    """
    if not (path / ".git").exists():
        return False
    _ = await run_git(path, "add", "-A")
    # Exits 0 when nothing is staged.
    clean, _, _ = await run_git(path, "diff", "--cached", "--quiet")
    if clean == 0:
        return False
    code, out, err = await run_git(path, *AUTHOR, "commit", "-m", message or "Agent changes")
    if code != 0:
        logger.warning("worktree: could not commit %s: %s", path, (err or out).strip())
        return False
    return True
