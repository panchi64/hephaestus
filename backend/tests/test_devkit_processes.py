"""The record of what is running, and who is allowed to forget it.

Every test here is about one property: an instance's services must stay reachable by
``stop``. That is easy to break precisely because ``up`` converges — most runs start
nothing, and a run that started nothing must not be allowed to say so in a way that
erases what an earlier run started. Both failures below were real.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from pydantic import TypeAdapter

from devkit import processes
from devkit.processes import Supervisor

from ._helpers import group_running

_PIDS = TypeAdapter(dict[str, int])


@pytest.fixture
def sleeper(tmp_path: Path) -> Iterator[Supervisor]:
    """A supervisor holding one real, harmless child in its own process group."""
    supervisor = Supervisor()
    _ = supervisor.start(
        "stub",
        [sys.executable, "-c", "import time; time.sleep(60)"],
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin"},
        log_dir=tmp_path / "logs",
    )
    yield supervisor
    supervisor.stop_all()


def _recorded(root: Path) -> dict[str, int]:
    return _PIDS.validate_json((root / processes.PIDFILE).read_text())


def test_a_run_that_started_nothing_leaves_the_record_alone(sleeper: Supervisor, tmp_path: Path):
    # The bug this pins: `up` calls record() unconditionally, so a converged run — which
    # starts nothing — wrote an empty file over the first run's groups, and `stop` then
    # reported "nothing recorded" while three services kept running.
    _ = sleeper.record(tmp_path)
    assert set(_recorded(tmp_path)) == {"stub"}

    _ = Supervisor().record(tmp_path)  # a second `up` that found everything already up
    assert set(_recorded(tmp_path)) == {"stub"}


def test_a_later_run_adds_to_the_record_rather_than_replacing_it(
    sleeper: Supervisor, tmp_path: Path
):
    _ = sleeper.record(tmp_path)
    second = Supervisor()
    _ = second.start(
        "frontend",
        [sys.executable, "-c", "import time; time.sleep(60)"],
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin"},
        log_dir=tmp_path / "logs",
    )
    try:
        _ = second.record(tmp_path)
        assert set(_recorded(tmp_path)) == {"stub", "frontend"}
    finally:
        second.stop_all()


def test_forget_drops_only_what_this_run_started(sleeper: Supervisor, tmp_path: Path):
    # The foreground path's teardown. A plain `up` that converged onto a detached
    # instance owns nothing, and deleting the file on its way out stranded the detached
    # services with no way to stop them.
    _ = sleeper.record(tmp_path)
    Supervisor().forget(tmp_path)
    assert set(_recorded(tmp_path)) == {"stub"}


def test_forget_removes_the_file_once_nothing_is_left(sleeper: Supervisor, tmp_path: Path):
    _ = sleeper.record(tmp_path)
    sleeper.forget(tmp_path)
    assert not (tmp_path / processes.PIDFILE).exists()


def test_a_dead_group_is_pruned_from_the_record(sleeper: Supervisor, tmp_path: Path):
    stale = {"backend": 999_999}  # a pid nothing could plausibly be using
    _ = (tmp_path / processes.PIDFILE).write_text(json.dumps(stale))
    _ = sleeper.record(tmp_path)
    assert set(_recorded(tmp_path)) == {"stub"}


def test_an_unreadable_record_is_treated_as_absent(sleeper: Supervisor, tmp_path: Path):
    _ = (tmp_path / processes.PIDFILE).write_text("{ not json")
    _ = sleeper.record(tmp_path)
    assert set(_recorded(tmp_path)) == {"stub"}


def test_stop_recorded_kills_the_whole_group_and_clears_the_file(tmp_path: Path):
    supervisor = Supervisor()
    # A child that spawns its own child: signalling only the handle we hold would leave
    # the grandchild running with the port still taken, which the next `up` reads as an
    # instance that is already healthy.
    _ = supervisor.start(
        "backend",
        [
            sys.executable,
            "-c",
            "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',"
            + "'import time; time.sleep(60)']); time.sleep(60)",
        ],
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin"},
        log_dir=tmp_path / "logs",
    )
    group = processes._group_of(supervisor.services[0])
    assert group is not None
    _ = supervisor.record(tmp_path)

    assert processes.stop_recorded(tmp_path) == ["backend"]
    assert not (tmp_path / processes.PIDFILE).exists()
    assert not group_running(group)


def test_stopping_an_instance_that_was_never_started_is_not_an_error(tmp_path: Path):
    assert processes.stop_recorded(tmp_path) == []


@pytest.mark.usefixtures("sleeper")
def test_the_log_handle_is_not_left_open_in_this_process(tmp_path: Path):
    # Popen dups the descriptor for the child, so the parent's copy is pure leak — a
    # caller that started services in a loop would run out of descriptors.
    log = tmp_path / "logs" / "stub.log"
    assert log.is_file()
    open_here = subprocess.run(["lsof", "-p", str(os.getpid())], capture_output=True, text=True)
    if open_here.returncode != 0:  # lsof is not everywhere; the assertion below is
        pytest.skip("lsof unavailable")
    assert str(log) not in open_here.stdout


def test_signalling_a_group_that_has_already_gone_is_silent():
    processes.signal_group(None, signal.SIGTERM)
    processes.signal_group(999_999, signal.SIGTERM)
