"""Worktree branches from before the rename move into the ``heph/`` namespace."""

from __future__ import annotations

import subprocess
from pathlib import Path

from services.projects.legacy_branches import rename_legacy_branches


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.email=a@b", "-c", "user.name=a", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


async def test_old_branches_are_renamed_and_their_worktrees_follow(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _ = _git(repo, "init", "-q")
    _ = _git(repo, "commit", "-q", "--allow-empty", "-m", "init")
    _ = _git(repo, "worktree", "add", "-q", "-b", "ody/conv-a", str(tmp_path / "tree"))
    _ = _git(repo, "branch", "ody/conv-a-child")
    _ = _git(repo, "branch", "feature")

    renamed = await rename_legacy_branches(repo)

    assert sorted(renamed) == ["heph/conv-a", "heph/conv-a-child"]
    assert _git(tmp_path / "tree", "branch", "--show-current") == "heph/conv-a"
    assert "feature" in _git(repo, "branch", "--list", "feature")


async def test_a_directory_that_is_not_a_repository_is_left_alone(tmp_path: Path):
    assert await rename_legacy_branches(tmp_path) == []
