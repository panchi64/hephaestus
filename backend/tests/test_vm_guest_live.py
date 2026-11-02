"""The guest contract, checked in a real machine booted from the built image.

Every other VM test runs on fakes, which cannot show who a command runs as or what git
does in a mount. Skipped unless ``HEPHAESTUS_VM_LIVE_IMAGES`` names an images directory
holding the image the current ``vm/build_image.sh`` builds (a dev instance's
``data/vm/images``).
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
from pydantic_ai.workspaces import CommandResult

from services.home.backend import Mount
from services.home.paths import SCRATCH, Place
from services.home.table import RunHome
from services.home.workspace import machine_spec
from services.projects.repo import common_dir
from services.vm import ConversationMachines, GuestImage, Platform

_IMAGES = os.environ.get("HEPHAESTUS_VM_LIVE_IMAGES")
_IMAGE = GuestImage(Path(_IMAGES)).path if _IMAGES else None

pytestmark = pytest.mark.skipif(
    _IMAGE is None or not _IMAGE.is_file(),
    reason="set HEPHAESTUS_VM_LIVE_IMAGES to an images dir holding the current guest image",
)

CHECKOUT = Place("projects", "app")
WORKTREE = Place("worktrees", "app")
CHILD = Place("worktrees", "app-fix")


def _git(cwd: Path, *args: str) -> None:
    _ = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


def _home(tmp_path: Path) -> RunHome:
    """A checkout, a worktree of it and a sub-agent's worktree off that one, cut from the
    project root as the platform cuts them (`services/projects/fork.py`)."""
    root = (tmp_path / "app").resolve()
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _ = (root / "a.txt").write_text("a\n")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "init")
    worktree, child = tmp_path / "wt", tmp_path / "child"
    _git(root, "worktree", "add", "-q", "-b", "heph/parent", str(worktree), "HEAD")
    _git(root, "worktree", "add", "-q", "-b", "heph/child", str(child), "heph/parent")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    git_dir = common_dir(root)
    return RunHome(
        mounts=(
            Mount(SCRATCH, scratch, writable=True),
            Mount(CHECKOUT, root, writable=True, project_id="p"),
            Mount(WORKTREE, worktree.resolve(), writable=True, project_id="p", git_dir=git_dir),
            Mount(CHILD, child.resolve(), writable=True, project_id="p", git_dir=git_dir),
        ),
        default=WORKTREE,
        key="live",
    )


@pytest.fixture
async def machines() -> AsyncGenerator[ConversationMachines]:
    assert _IMAGE is not None
    platform = Platform(GuestImage(_IMAGE.parent.parent, override=_IMAGE))
    await platform.start()
    assert await platform.wait_ready(60.0), platform.failure
    pool = ConversationMachines(
        platform,
        prefix="heph-live-test",
        cpus=1,
        memory_mb=1024,
        idle_ttl_s=600.0,
        reap_interval_s=600.0,
        max_machines=1,
    )
    try:
        yield pool
    finally:
        await pool.release_all()


async def _sh(pool: ConversationMachines, home: RunHome, place: Place, line: str) -> CommandResult:
    async with pool.use("live", machine_spec(home, None), working_dir=place.path) as vm:
        return await vm.run(line, shell=True, timeout=120)


def _ok(result: CommandResult) -> str:
    assert result.exit_code == 0, f"exit {result.exit_code}: {result.stderr or result.stdout}"
    return result.stdout.strip()


async def test_commands_run_as_the_host_owner(machines: ConversationMachines, tmp_path: Path):
    home = _home(tmp_path)
    out = _ok(await _sh(machines, home, SCRATCH, "id -u; id -g; whoami; echo $HOME"))
    assert out.split() == [str(os.getuid()), str(os.getgid()), "agent", "/home/agent"]


async def test_what_a_command_writes_is_the_operators(
    machines: ConversationMachines, tmp_path: Path
):
    home = _home(tmp_path)
    for place in (SCRATCH, CHECKOUT, WORKTREE, CHILD):
        _ = _ok(await _sh(machines, home, place, "echo hi > made.txt"))
        mount = home.mount(place)
        assert mount is not None
        assert (mount.root / "made.txt").stat().st_uid == os.getuid(), place
    _ = _ok(await _sh(machines, home, SCRATCH, "mkdir -p ~/.cache/probe && touch ~/.cache/probe/x"))


@pytest.mark.parametrize(
    "place", [CHECKOUT, WORKTREE, CHILD], ids=["checkout", "worktree", "child"]
)
async def test_git_reads_in_every_kind_of_repository(
    machines: ConversationMachines, tmp_path: Path, place: Place
):
    home = _home(tmp_path)
    _ = _ok(await _sh(machines, home, place, "echo b >> a.txt"))
    assert "a.txt" in _ok(await _sh(machines, home, place, "git status --short"))
    assert "+b" in _ok(await _sh(machines, home, place, "git diff"))
    assert "init" in _ok(await _sh(machines, home, place, "git log --oneline -1"))


async def test_git_does_not_write_a_worktree_the_platform_commits(
    machines: ConversationMachines, tmp_path: Path
):
    home = _home(tmp_path)
    commit = "echo b >> a.txt && git -c user.name=t -c user.email=t@t commit -qam t"
    result = await _sh(machines, home, WORKTREE, commit)
    assert result.exit_code != 0
    assert "Read-only file system" in result.stderr


async def test_sudo_needs_no_password(machines: ConversationMachines, tmp_path: Path):
    assert _ok(await _sh(machines, _home(tmp_path), SCRATCH, "sudo -n id -u")) == "0"
