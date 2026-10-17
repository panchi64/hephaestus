"""The judge's reading of paths against the agent's home."""

from __future__ import annotations

from services.home.paths import SCRATCH, Place
from services.permissions import capability_of, judge
from services.permissions.capability import shell_capability
from services.permissions.fence import HomeFence

API = Place("projects", "api")
WEB = Place("projects", "web")


def _fence(cwd: str = "/home/agent/scratch") -> HomeFence:
    return HomeFence(
        places=frozenset({SCRATCH, API, WEB}), writable=frozenset({SCRATCH, API}), cwd=cwd
    )


def _escapes(command: str, cwd: str = "/home/agent/scratch") -> tuple[str, ...]:
    return shell_capability("shell_run_command", command, root=_fence(cwd)).escapes


def test_scratch_and_its_alias_are_inside() -> None:
    assert _escapes("cat ~/scratch/a.txt /work/b.txt ./c/d.txt") == ()


def test_a_command_reads_any_place_the_thread_holds() -> None:
    assert _escapes("diff ~/projects/api/a.py ~/projects/web/a.py") == ()
    assert _escapes("cat ../web/README.md", cwd="/home/agent/projects/api") != ()


def test_a_commands_own_tmpfs_and_streams_are_inside() -> None:
    # Measured: `python3 fizzbuzz.py | tee /tmp/out.txt` parked a build task for the
    # operator, though the guest's /tmp is the command's own and dropped when it ends.
    assert _escapes("python3 a.py | tee /tmp/out.txt > /dev/null 2>/dev/stderr") == ()
    assert _escapes("cat /tmp/../etc/shadow") == ("/tmp/../etc/shadow",)


def test_an_offline_command_in_scratch_is_bounded_by_the_vm() -> None:
    # Measured: subshells in a build task's test run parked for the operator, where the
    # same work through the old sandbox tool was cleared on its container.
    def judged(command: str, *, cwd: str = "/home/agent/scratch", reach: str = "workspace"):
        capability = capability_of(
            "shell_run_command", {"command": command, "reach": reach}, root=_fence(cwd)
        )
        return judge(capability, fenced=True), judge(capability, fenced=False)

    on, off = judged("cd /work && (python -m pytest -q || true)")
    assert on.approved and on.tier == "sandbox" and not off.approved
    # Not once it names or stands in a project, or asks for the network.
    assert not judged("(cat ~/projects/api/x)")[0].approved
    assert not judged("(ls)", cwd="/home/agent/projects/api")[0].approved
    assert not judged("(pip install x)", reach="network")[0].approved


def test_a_file_write_to_tmp_still_escapes() -> None:
    write = capability_of("files_write_file", {"path": "/tmp/x"}, root=_fence())
    assert write.escapes


def test_paths_outside_the_places_escape() -> None:
    assert _escapes("cat /etc/passwd") == ("/etc/passwd",)
    assert _escapes("cat ~/projects/other/x") == ("~/projects/other/x",)
    assert _escapes("ls ..") == ("..",)
    assert _escapes("cat ~/.ssh/id_ed25519") == ("~/.ssh/id_ed25519",)


def test_a_file_write_escapes_a_place_the_thread_may_only_read() -> None:
    def escapes(path: str) -> bool:
        return bool(capability_of("files_write_file", {"path": path}, root=_fence()).escapes)

    assert not escapes("~/projects/api/a.py")
    assert escapes("~/projects/web/a.py")
    assert not escapes("notes.md")
