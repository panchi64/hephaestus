"""What the guest's ``output`` action reads from a background job's files, and when it
removes them. Driven against a temp root, so no bubblewrap is needed."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from pydantic import BaseModel

CONFINE = Path(__file__).resolve().parent.parent / "vm" / "guest" / "confine.py"


class _Output(BaseModel):
    running: bool
    exit: int | None
    out: str
    err: str
    out_end: int
    err_end: int


def _jobs(root: Path) -> Path:
    jobs = root / "run" / "hephaestus" / "jobs"
    jobs.mkdir(parents=True, exist_ok=True)
    return jobs


def _output(root: Path, *flags: str) -> _Output:
    done = subprocess.run(
        [sys.executable, str(CONFINE), "output", "--root", str(root), "--name", "bg-1", *flags],
        capture_output=True,
        text=True,
        check=True,
    )
    return _Output.model_validate_json(done.stdout)


def test_a_capped_read_keeps_the_start_unless_it_is_asked_for_the_end(tmp_path: Path) -> None:
    jobs = _jobs(tmp_path)
    _ = (jobs / "bg-1.out").write_text("0123456789")
    _ = (jobs / "bg-1.err").write_text("")

    head = _output(tmp_path, "--limit", "4")
    tail = _output(tmp_path, "--limit", "4", "--tail")

    assert head.out == "0123" and head.out_end == 4
    assert tail.out == "6789" and tail.out_end == 10


def test_a_finished_jobs_files_go_once_all_of_them_has_been_read(tmp_path: Path) -> None:
    jobs = _jobs(tmp_path)
    _ = (jobs / "bg-1.out").write_text("0123456789")
    _ = (jobs / "bg-1.err").write_text("oops")
    _ = (jobs / "bg-1.pid").write_text("1")
    _ = (jobs / "bg-1.exit").write_text("3")

    partial = _output(tmp_path, "--limit", "4")
    assert not partial.running and partial.exit == 3
    assert (jobs / "bg-1.out").exists()

    last = _output(tmp_path, "--out-offset", "4", "--limit", "100")
    assert last.out == "456789" and last.err == "oops"
    assert not any(jobs.iterdir())


def test_a_finished_job_ending_mid_character_is_still_cleaned_up(tmp_path: Path) -> None:
    jobs = _jobs(tmp_path)
    _ = (jobs / "bg-1.out").write_bytes("ok é".encode()[:-1])
    _ = (jobs / "bg-1.err").write_text("")
    _ = (jobs / "bg-1.exit").write_text("0")

    state = _output(tmp_path)

    assert state.out == "ok "
    assert not any(jobs.iterdir())


def test_a_running_jobs_files_stay_after_a_complete_read(tmp_path: Path) -> None:
    jobs = _jobs(tmp_path)
    _ = (jobs / "bg-1.out").write_text("so far")
    _ = (jobs / "bg-1.err").write_text("")

    state = _output(tmp_path)

    assert state.running and state.out == "so far"
    assert (jobs / "bg-1.out").exists()
