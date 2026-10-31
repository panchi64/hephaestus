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

from core.config import get_settings
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


#: Flags every host `git diff` carries: neither an external diff nor a textconv driver
#: runs, whatever config or `.gitattributes` names.
_DIFF_PINS = ("--no-ext-diff", "--no-textconv")


def linked_git_dir(root: Path, tree: Path) -> Path | None:
    """The admin directory ``root`` keeps for its linked worktree at ``tree``, or None.

    Found from the root's side: a worktree's own `.git` file is writable by whatever runs in
    the worktree, and a redirected one would hand git a config its author wrote.
    """
    common = _common_dir(root)
    admin = common / "worktrees" if common is not None else None
    if admin is None or not admin.is_dir():
        return None
    want = (tree.resolve() / ".git").resolve()
    for entry in admin.iterdir():
        try:
            points_at = (entry / "gitdir").read_text().strip()
        except OSError:
            continue
        # `worktree.useRelativePaths` writes the path relative to the admin entry.
        if (entry / points_at).resolve() == want:
            return entry
    return None


def _common_dir(root: Path) -> Path | None:
    """The git directory ``root``'s worktrees register under: its own `.git`, or, when the
    operator's project is itself a linked worktree or a submodule, the one it links to."""
    dot = root / ".git"
    if dot.is_dir():
        return dot
    try:
        text = dot.read_text().strip()
    except OSError:
        return None
    if not text.startswith("gitdir:"):
        return None
    git_dir = (root / text.removeprefix("gitdir:").strip()).resolve()
    try:
        return (git_dir / (git_dir / "commondir").read_text().strip()).resolve()
    except OSError:
        return git_dir


def _location(tree: Path, root: Path | None) -> tuple[str, ...] | None:
    """The `--git-dir`/`--work-tree` pair for ``tree``, or None when it can't be trusted.

    A tree under the worktrees directory is a linked worktree and resolves only through
    ``root``. Any other tree is the operator's: an explicit pair when its `.git` is a
    directory, git's own discovery otherwise (a submodule or their own linked worktree).
    """
    if root is not None and tree.resolve() != root.resolve():
        git_dir = linked_git_dir(root, tree)
        if git_dir is None:
            return None
        return (f"--git-dir={git_dir}", f"--work-tree={tree}")
    if tree.resolve().is_relative_to(get_settings().worktrees_dir):
        return None
    if (tree / ".git").is_dir():
        return (f"--git-dir={tree / '.git'}", f"--work-tree={tree}")
    return ()


def _with_diff_pins(args: tuple[str, ...]) -> tuple[str, ...]:
    """``args`` with :data:`_DIFF_PINS` after the subcommand when it is `diff`."""
    i = 0
    while i < len(args) and args[i] == "-c":
        i += 2
    if i < len(args) and args[i] == "diff":
        return (*args[: i + 1], *_DIFF_PINS, *args[i + 1 :])
    return args


async def run_git(tree: Path, *args: str, root: Path | None = None) -> tuple[int, str, str]:
    """One git invocation in ``tree`` with a fixed argv and no shell, so a project path is
    never word-split or interpolated.

    ``root`` is the project a linked worktree belongs to, and is required for one: the git
    directory is read from the root's side (:func:`linked_git_dir`). A tree that can't be
    placed, or a missing one, returns a failed call rather than raising, since the operator
    can move a project directory at any time and callers handle "git said no".
    """
    # Off the event loop: placing a linked worktree scans the root's admin directories.
    location = await asyncio.to_thread(_location, tree, root)
    if location is None:
        return 1, "", f"{tree} is not a worktree this project registered"
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            *GIT_ARG_PINS,
            *location,
            *_with_diff_pins(args),
            cwd=str(tree),
            env=git_config_pins(os.environ),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (OSError, NotADirectoryError) as exc:
        return 1, "", f"{tree} is not reachable: {exc}"
    out, err = await proc.communicate()
    return (
        proc.returncode or 0,
        out.decode("utf-8", "replace"),
        err.decode("utf-8", "replace"),
    )


async def run_git_ok(tree: Path, *args: str, root: Path | None = None) -> str:
    code, out, err = await run_git(tree, *args, root=root)
    if code != 0:
        raise WorktreeError((err or out).strip() or f"git {' '.join(args)} failed")
    return out


async def commit_worktree(path: Path, *, root: Path, message: str | None = None) -> bool:
    """Stage and commit everything in ``path``, a worktree of ``root`` or ``root`` itself,
    or return False if it was clean.

    Best-effort by design: a worktree that isn't there, or a git that refuses, must
    not take down the read that asked for this. `.hephaestus/` ignores itself, so the
    agent's staged attachments and skill bundles never reach the operator's diff.
    """
    if not (path / ".git").exists():
        return False
    _ = await run_git(path, "add", "-A", root=root)
    # Exits 0 when nothing is staged.
    clean, _, _ = await run_git(path, "diff", "--cached", "--quiet", root=root)
    if clean == 0:
        return False
    code, out, err = await run_git(
        path, *AUTHOR, "commit", "-m", message or "Agent changes", root=root
    )
    if code != 0:
        logger.warning("worktree: could not commit %s: %s", path, (err or out).strip())
        return False
    return True
