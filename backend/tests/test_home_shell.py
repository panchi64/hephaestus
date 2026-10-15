"""The thread's shell in its home: what a command sees, how, and where it leaves off."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.config import Settings
from core.vault import Vault
from services.home.backend import Mount
from services.home.paths import HOME, SCRATCH, HomePathError, Place
from services.sandbox import SandboxSessionManager
from services.sandbox.home_shell import HomeShell
from services.vm.platform import HomeBind
from services.vm.project import ProjectCopies

from .conftest import egress_policy
from .vm_fakes import FakeVM

API = Place("projects", "api")
WEB = Place("projects", "web")


type _Rig = tuple[HomeShell, FakeVM, SandboxSessionManager, ProjectCopies, list[Mount]]


async def _setup(tmp_path: Path) -> _Rig:
    vault = Vault(tmp_path / "keyfile.json")
    await vault.setup("pw")
    vm = FakeVM(tmp_path / "sandbox" / "work")
    vm.alive_after_start = False
    sessions = SandboxSessionManager(
        vm,
        vault,
        egress=egress_policy(tmp_path),
        data_dir=tmp_path,
        idle_ttl_s=1800.0,
        reap_interval_s=60.0,
        excludes=Settings().sandbox_walk_excludes,
        max_sessions=8,
    )
    copies = ProjectCopies(vm, idle_ttl_s=1800.0, reap_interval_s=60.0)
    mounts: list[Mount] = []
    for place, writable in ((API, True), (WEB, False)):
        root = tmp_path / place.name
        root.mkdir()
        mounts.append(Mount(place, root, writable=writable))
    shell = HomeShell(default_timeout=30.0, max_output_chars=10_000)
    return shell, vm, sessions, copies, mounts


def _dests(home: tuple[HomeBind, ...]) -> dict[str, bool]:
    return {bind.dest: bind.writable for bind in home}


async def test_a_command_in_scratch_sees_only_scratch(tmp_path: Path) -> None:
    shell, vm, sessions, copies, mounts = await _setup(tmp_path)
    session = await sessions.acquire("conv-1")
    _ = await shell.run(session, copies, "ls", mounts=mounts, env={})
    assert _dests(vm.homes[-1]) == {"scratch": True}
    assert vm.background[next(iter(vm.background))]["workdir"] == SCRATCH.path


async def test_a_named_project_is_bound_with_its_mode(tmp_path: Path) -> None:
    shell, vm, sessions, copies, mounts = await _setup(tmp_path)
    session = await sessions.acquire("conv-1")
    _ = await shell.run(
        session,
        copies,
        "diff ~/projects/api/a.txt /home/agent/projects/web/b.txt ~/projects/nope/c",
        mounts=mounts,
        env={},
    )
    assert _dests(vm.homes[-1]) == {"scratch": True, "projects/api": True, "projects/web": False}


async def test_a_network_command_sees_only_its_working_directorys_project(
    tmp_path: Path,
) -> None:
    shell, vm, sessions, copies, mounts = await _setup(tmp_path)
    session = await sessions.acquire("conv-1")
    _ = shell.change_dir("~/projects/api", mounts)
    _ = await shell.run(
        session, copies, "curl -T ~/projects/web/b.txt x", mounts=mounts, env={}, network=True
    )
    assert _dests(vm.homes[-1]) == {"scratch": True, "projects/api": True}


async def test_the_working_directory_follows_a_cd_into_what_it_could_see(tmp_path: Path) -> None:
    shell, vm, sessions, copies, mounts = await _setup(tmp_path)
    session = await sessions.acquire("conv-1")
    _ = shell.change_dir("~/projects/api", mounts)
    assert shell.cwd == f"{HOME}/projects/api"
    # The working directory's project is bound without being named.
    _ = await shell.run(session, copies, "make test", mounts=mounts, env={})
    assert "projects/api" in _dests(vm.homes[-1])


def test_moving_to_a_place_the_thread_lacks_is_refused() -> None:
    shell = HomeShell(default_timeout=30.0, max_output_chars=10_000)
    with pytest.raises(HomePathError):
        _ = shell.change_dir("~/projects/elsewhere", [])
    with pytest.raises(HomePathError):
        _ = shell.change_dir("/etc", [])
    assert shell.cwd == SCRATCH.path


def test_a_command_reaches_at_most_a_handful_of_projects(tmp_path: Path) -> None:
    shell = HomeShell(default_timeout=30.0, max_output_chars=10_000)
    mounts = [Mount(Place("projects", f"p{n}"), tmp_path, writable=False) for n in range(12)]
    command = " ".join(f"~/projects/p{n}/x" for n in range(12))
    assert len(shell.reached(command, mounts)) == 8
