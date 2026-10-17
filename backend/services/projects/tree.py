"""Which tree a project thread is looking at, asked of the filesystem and never creating
one — so a picker or a tool description can read a project's files without taking its
checkout or cutting a worktree."""

from __future__ import annotations

from pathlib import Path

from .repo import branch_for, run_git


async def worktree_root(
    worktree_path: Path, conversation_id: str, project_root: Path
) -> tuple[Path, bool]:
    """Which filesystem the picker should list, and whether it is the thread's worktree.

    **Not the holder map.** ``WorktreeManager._holders`` is an in-memory dict written only
    by ``acquire``, so after a backend restart a worktree that is still very much on disk
    has no holder — and a picker keyed on one would quietly fall back to the operator's own
    checkout while the agent's first file call re-acquires the worktree. The operator would
    pick ``src/foo.ts`` from a tree the agent cannot see, and nothing would report it.

    So the question is asked of the filesystem, which is where the answer actually lives:
    the worktree directory exists, and the branch checked out in it is this conversation's.
    Both halves matter — one worktree serves a project, so an existing directory on
    *another* thread's branch is not this thread's files.

    **It never creates one.** A picker that acquired a checkout as a side effect of typing
    ``@`` would take the project's single worktree away from whatever else holds it. Before
    a thread's first turn there is nothing to list but the project root, which is the
    honest answer and also the useful one.
    """
    if not worktree_path.is_dir():
        return project_root, False
    code, out, _ = await run_git(worktree_path, "rev-parse", "--abbrev-ref", "HEAD")
    if code != 0 or out.strip() != branch_for(conversation_id):
        return project_root, False
    return worktree_path, True


async def thread_tree(
    project_root: Path, worktree_path: Path, worktree: bool, conversation_id: str | None
) -> Path:
    """The tree a thread works in: the project's own folder, or the thread's worktree once
    it exists."""
    if not worktree or conversation_id is None:
        return project_root
    tree, _ = await worktree_root(worktree_path, conversation_id, project_root)
    return tree
