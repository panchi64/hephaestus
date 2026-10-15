"""The thread's shell in its home: what a command sees, how, and where it leaves off."""

from __future__ import annotations

from pathlib import Path

from core.config import Settings
from core.vault import Vault
from services.home.backend import Mount
from services.home.paths import HOME, SCRATCH, Place
from services.sandbox import SandboxSessionManager
from services.sandbox.home_shell import HomeShell
from services.vm.platform import HomeBind
from services.vm.project import ProjectCopies

from .conftest import egress_policy
from .vm_fakes import FakeVM

API = Place("projects", "api")
WEB = Place("projects", "web")


type _Rig = tuple[FakeVM, SandboxSessionManager, ProjectCopies, list[Mount]]


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
    return vm, sessions, copies, mounts


def _shell(start: str = SCRATCH.path) -> HomeShell:
    return HomeShell(default_timeout=30.0, max_output_chars=10_000, start=start)


def _dests(home: tuple[HomeBind, ...]) -> dict[str, bool]:
    return {bind.dest: bind.writable for bind in home}


async def test_a_command_in_scratch_sees_only_scratch(tmp_path: Path) -> None:
    vm, sessions, copies, mounts = await _setup(tmp_path)
    session = await sessions.acquire("conv-1")
    _ = await _shell().run(session, copies, "ls", mounts=mounts)
    assert _dests(vm.homes[-1]) == {"scratch": True}
    assert vm.background[next(iter(vm.background))]["workdir"] == SCRATCH.path


async def test_a_named_project_is_bound_with_its_mode(tmp_path: Path) -> None:
    vm, sessions, copies, mounts = await _setup(tmp_path)
    session = await sessions.acquire("conv-1")
    _ = await _shell().run(
        session,
        copies,
        "diff ~/projects/api/a.txt /home/agent/projects/web/b.txt ~/projects/nope/c",
        mounts=mounts,
    )
    assert _dests(vm.homes[-1]) == {"scratch": True, "projects/api": True, "projects/web": False}


async def test_a_network_command_sees_only_its_working_directorys_project(
    tmp_path: Path,
) -> None:
    vm, sessions, copies, mounts = await _setup(tmp_path)
    session = await sessions.acquire("conv-1")
    shell = _shell(f"{HOME}/projects/api")
    _ = await shell.run(
        session, copies, "curl -T ~/projects/web/b.txt x", mounts=mounts, network=True
    )
    assert _dests(vm.homes[-1]) == {"scratch": True, "projects/api": True}


async def test_a_fresh_sessions_first_network_command_gets_the_proxy(tmp_path: Path) -> None:
    # The egress credentials are granted by the session's bring-in, so an environment read
    # before it would send the first `pip install` out with no proxy at all.
    vm, sessions, copies, mounts = await _setup(tmp_path)
    session = await sessions.acquire("conv-1")
    _ = await _shell().run(session, copies, "pip install x", mounts=mounts, network=True)
    env = vm.background[next(iter(vm.background))]["env"]
    assert isinstance(env, dict) and "HTTPS_PROXY" in env


async def test_the_working_directorys_project_is_bound_without_being_named(
    tmp_path: Path,
) -> None:
    vm, sessions, copies, mounts = await _setup(tmp_path)
    session = await sessions.acquire("conv-1")
    _ = await _shell(f"{HOME}/projects/api").run(session, copies, "make test", mounts=mounts)
    assert "projects/api" in _dests(vm.homes[-1])


async def test_a_finished_background_command_is_retired_once(tmp_path: Path) -> None:
    # A finished job kept on the books brought its places back on every later check, and
    # kept its project copy from ever being reaped.
    _vm, sessions, copies, mounts = await _setup(tmp_path)
    session = await sessions.acquire("conv-1")
    shell = _shell(f"{HOME}/projects/api")
    command_id = await shell.start(session, copies, "npm test", mounts=mounts)
    first = await shell.check(copies, command_id)
    second = await shell.check(copies, command_id)
    assert first is not None and first.status == "finished"
    assert second == first
    assert not shell.busy
    assert all(not copy.jobs for copy in (copies.acquire(m.root) for m in mounts))


def test_a_command_reaches_at_most_a_handful_of_projects(tmp_path: Path) -> None:
    mounts = [Mount(Place("projects", f"p{n}"), tmp_path, writable=False) for n in range(12)]
    command = " ".join(f"~/projects/p{n}/x" for n in range(12))
    assert len(_shell().reached(command, mounts)) == 8
