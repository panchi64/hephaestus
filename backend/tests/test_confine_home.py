"""The agent's home in the guest: which folders a command sees there, and how.

Read off ``confine.py --dry-run``, which prints the bubblewrap command a run would use, so
the layout is checked on any machine; the confinement itself is exercised in
``test_vm_boundary.py`` where bubblewrap and root are available.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import TypeAdapter

CONFINE = Path(__file__).resolve().parent.parent / "vm" / "guest" / "confine.py"
_ARGV: TypeAdapter[list[str]] = TypeAdapter(list[str])


def _plan(*flags: str, root: Path) -> subprocess.CompletedProcess[str]:
    argv = [sys.executable, str(CONFINE), "run", "--root", str(root), *flags, "--dry-run"]
    return subprocess.run(
        [*argv, "--", "true"],
        capture_output=True,
        text=True,
        check=False,
    )


def _argv(*flags: str, root: Path) -> list[str]:
    done = _plan(*flags, root=root)
    assert done.returncode == 0, done.stderr
    return _ARGV.validate_json(done.stdout)


def _binds(argv: list[str]) -> list[tuple[str, str, str]]:
    return [
        (argv[at], argv[at + 1], argv[at + 2])
        for at, flag in enumerate(argv)
        if flag in ("--bind", "--ro-bind")
    ]


def _env(argv: list[str], name: str) -> str:
    at = next(i for i, flag in enumerate(argv) if flag == "--setenv" and argv[i + 1] == name)
    return argv[at + 2]


def test_without_a_home_the_workspace_is_all_a_command_sees(tmp_path: Path) -> None:
    argv = _argv("--key", "s-thread", root=tmp_path)
    assert ("--bind", str(tmp_path / "work" / "s-thread"), "/work") in _binds(argv)
    assert not any(dest.startswith("/home") for _, _, dest in _binds(argv))
    assert _env(argv, "HOME") == "/work"


def test_a_home_binds_each_folder_at_its_place_with_its_mode(tmp_path: Path) -> None:
    argv = _argv(
        "--key",
        "s-thread",
        "--home-bind",
        "s-thread:scratch:rw",
        "--home-bind",
        "p-api:projects/api:ro",
        "--home-bind",
        "w-api:worktrees/api-fix:rw",
        "--workdir",
        "/home/agent/projects/api",
        root=tmp_path,
    )
    work = tmp_path / "work"
    binds = _binds(argv)
    assert ("--bind", str(work / "s-thread"), "/home/agent/scratch") in binds
    assert ("--ro-bind", str(work / "p-api"), "/home/agent/projects/api") in binds
    assert ("--bind", str(work / "w-api"), "/home/agent/worktrees/api-fix") in binds
    # Scratch stays at /work as well, for everything that already says /work.
    assert ("--bind", str(work / "s-thread"), "/work") in binds
    assert _env(argv, "HOME") == "/home/agent"
    assert argv[argv.index("--chdir") + 1] == "/home/agent/projects/api"
    # Measured: left to bubblewrap, `~/projects` came up root-owned 0700, so a command
    # standing in a project could not reach `~/projects/<name>` by its absolute path.
    for group in ("projects", "worktrees"):
        at = argv.index(f"/home/agent/{group}")
        assert argv[at - 3 : at] == ["--perms", "0755", "--dir"]
        assert at < argv.index(
            f"/home/agent/{group}/" + ("api" if group == "projects" else "api-fix")
        )


@pytest.mark.parametrize(
    "spec",
    [
        "p-api:projects/../etc:rw",
        "p-api:/etc:rw",
        "p-api:elsewhere/api:rw",
        "p-api:projects/api:rwx",
        "../escape:projects/api:ro",
        "p-api:projects/api/nested:ro",
    ],
)
def test_a_malformed_home_bind_is_refused(tmp_path: Path, spec: str) -> None:
    done = _plan("--key", "s-thread", "--home-bind", spec, root=tmp_path)
    assert done.returncode != 0


def test_one_place_cannot_be_bound_twice(tmp_path: Path) -> None:
    done = _plan(
        "--key",
        "s-thread",
        "--home-bind",
        "p-a:projects/api:ro",
        "--home-bind",
        "p-b:projects/api:rw",
        root=tmp_path,
    )
    assert done.returncode != 0


def test_the_working_directory_stays_inside_what_is_bound(tmp_path: Path) -> None:
    homed = ("--key", "s-thread", "--home-bind", "s-thread:scratch:rw")
    assert _plan(*homed, "--workdir", "/etc", root=tmp_path).returncode != 0
    assert _plan(*homed, "--workdir", "/home/agent/../root", root=tmp_path).returncode != 0
    # Without a home, an absolute path into it is no more allowed than any other.
    assert _plan("--key", "s-thread", "--workdir", "/home/agent", root=tmp_path).returncode != 0
