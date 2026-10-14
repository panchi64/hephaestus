"""A project folder crossing into the platform VM and back: files, ignores, and commits."""

from __future__ import annotations

import subprocess
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from services.vm.driver import VMError
from services.vm.platform import PlatformVM
from services.vm.project import ProjectWorkspace

from .vm_local import can_confine, local_vm

pytestmark = pytest.mark.skipif(not can_confine(), reason="needs root and bubblewrap")


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    _ = _git(root, "init", "-q", "-b", "main")
    _ = _git(root, "config", "user.name", "Operator")
    _ = _git(root, "config", "user.email", "op@example.com")
    _ = (root / ".gitignore").write_text("node_modules/\n")
    _ = (root / "app.py").write_text("print('v1')\n")
    _ = _git(root, "add", ".")
    _ = _git(root, "commit", "-q", "-m", "first")
    _ = (root / "notes.md").write_text("untracked but not ignored\n")
    (root / "node_modules").mkdir()
    _ = (root / "node_modules" / "host-build.bin").write_text("built on the host\n")
    return root


@pytest.fixture
async def vm(tmp_path: Path) -> AsyncIterator[PlatformVM]:
    platform, _driver = await local_vm(tmp_path)
    yield platform
    await platform.stop()


async def _sh(vm: PlatformVM, key: str, script: str) -> str:
    result = await vm.run(key, ["sh", "-c", script], env={}, timeout_s=30)
    assert result.exit_code == 0, result.stderr
    return result.stdout


async def test_the_project_crosses_without_what_it_ignores(vm: PlatformVM, repo: Path):
    project = ProjectWorkspace(vm, "p-files", repo)
    await project.bring_in()
    listing = await _sh(vm, "p-files", "ls -a; git status --porcelain; git log --oneline")
    assert "app.py" in listing and "notes.md" in listing
    assert "node_modules" not in listing
    assert "first" in listing


async def test_edits_come_back_and_ignored_output_stays_in_the_guest(vm: PlatformVM, repo: Path):
    project = ProjectWorkspace(vm, "p-edit", repo)
    await project.bring_in()
    _ = await _sh(
        vm,
        "p-edit",
        "echo \"print('v2')\" > app.py; echo new > added.txt; rm notes.md;"
        + " mkdir -p node_modules && echo linux > node_modules/guest-build.bin",
    )
    report = await project.bring_out()
    assert (repo / "app.py").read_text() == "print('v2')\n"
    assert (repo / "added.txt").exists()
    assert not (repo / "notes.md").exists()
    assert not (repo / "node_modules" / "guest-build.bin").exists()
    assert (repo / "node_modules" / "host-build.bin").exists()
    assert report.files == 3


async def test_a_commit_in_the_guest_fast_forwards_the_branch(vm: PlatformVM, repo: Path):
    project = ProjectWorkspace(vm, "p-commit", repo)
    await project.bring_in()
    _ = await _sh(vm, "p-commit", "echo more >> app.py && git commit -qam 'agent change'")
    report = await project.bring_out()
    assert report.commits_landed
    assert _git(repo, "log", "-1", "--format=%s") == "agent change"
    assert _git(repo, "status", "--porcelain", "--untracked-files=no") == ""


async def test_a_rewritten_branch_stays_in_the_guest(vm: PlatformVM, repo: Path):
    before = _git(repo, "rev-parse", "HEAD")
    project = ProjectWorkspace(vm, "p-amend", repo)
    await project.bring_in()
    _ = await _sh(vm, "p-amend", "git commit -q --amend -m rewritten")
    report = await project.bring_out()
    assert not report.commits_landed
    assert any("rewritten" in note for note in report.notes)
    assert _git(repo, "rev-parse", "HEAD") == before


async def test_work_on_another_branch_is_reported_not_landed(vm: PlatformVM, repo: Path):
    before = _git(repo, "rev-parse", "HEAD")
    project = ProjectWorkspace(vm, "p-branch", repo)
    await project.bring_in()
    _ = await _sh(vm, "p-branch", "git checkout -qb side && git commit -q --allow-empty -m side")
    report = await project.bring_out()
    assert any("refs/heads/side" in note for note in report.notes)
    assert _git(repo, "rev-parse", "HEAD") == before


async def test_a_host_commit_reaches_the_guest_before_the_next_command(vm: PlatformVM, repo: Path):
    project = ProjectWorkspace(vm, "p-host", repo)
    await project.bring_in()
    _ = (repo / "app.py").write_text("print('operator')\n")
    _ = _git(repo, "commit", "-qam", "operator change")
    await project.bring_in()
    log = await _sh(vm, "p-host", "git log --oneline -1; git status --porcelain")
    assert "operator change" in log
    assert "app.py" not in log


async def test_a_symlink_out_of_the_project_is_refused(vm: PlatformVM, repo: Path):
    project = ProjectWorkspace(vm, "p-link", repo)
    await project.bring_in()
    _ = await _sh(vm, "p-link", "ln -s /etc/passwd leak")
    with pytest.raises(VMError):
        _ = await project.bring_out()
    assert not (repo / "leak").exists()


async def test_a_plain_folder_crosses_whole(vm: PlatformVM, tmp_path: Path):
    folder = tmp_path / "plain"
    folder.mkdir()
    _ = (folder / "data.csv").write_text("a,b\n")
    project = ProjectWorkspace(vm, "p-plain", folder)
    await project.bring_in()
    _ = await _sh(vm, "p-plain", "cat data.csv > copy.csv")
    _ = await project.bring_out()
    assert (folder / "copy.csv").read_text() == "a,b\n"


async def test_eviction_wipes_the_guest_and_keeps_the_work(vm: PlatformVM, repo: Path):
    project = ProjectWorkspace(vm, "p-evict", repo)
    await project.bring_in()
    _ = await _sh(vm, "p-evict", "echo kept > kept.txt")
    _ = await project.evict()
    assert (repo / "kept.txt").read_text() == "kept\n"
    assert not Path(vm.layout.work("p-evict")).exists()
