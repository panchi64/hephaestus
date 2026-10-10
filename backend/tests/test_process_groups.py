"""Shutdown only signals a process group it can still prove is its own.

A group id is its leader's pid, and once the group empties the OS may hand that number to
an unrelated session leader — the operator's own shell. ``stop_all_groups`` must never
signal such a number, and must still reach the real thing: a backgrounded child that
outlived the shell that started it.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from types import SimpleNamespace

import pytest

from services.sandbox import process

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")


@pytest.fixture(autouse=True)
def _clean_groups():
    process._groups.clear()
    yield
    process._groups.clear()


def _leader(returncode: int | None):
    return SimpleNamespace(returncode=returncode)


@pytest.fixture
def signals(monkeypatch):
    sent: list[tuple[int, int]] = []

    def killpg(pgid: int, sig: int) -> None:
        if sig != 0:
            sent.append((pgid, sig))

    monkeypatch.setattr(process.os, "killpg", killpg)
    return sent


async def test_a_reaped_leader_whose_pid_was_reused_is_never_signalled(monkeypatch, signals):
    # The group at this number "exists" and so does a process at it — but our leader is
    # reaped, so whatever holds the number now is someone else's.
    monkeypatch.setattr(process, "_pid_exists", lambda pid: True)
    process._groups[4242] = _leader(returncode=0)

    await process.stop_all_groups(grace_s=0.1)

    assert signals == []


async def test_an_unreaped_leader_still_holds_its_group(monkeypatch, signals):
    monkeypatch.setattr(process, "_pid_exists", lambda pid: True)
    process._groups[4242] = _leader(returncode=None)

    await process.stop_all_groups(grace_s=0.1)

    assert (4242, signal.SIGTERM) in signals


async def test_a_group_that_emptied_is_not_signalled(monkeypatch, signals):
    # Nothing left at the number: nothing to signal, whoever claims it later.
    def killpg(pgid: int, sig: int) -> None:
        raise ProcessLookupError

    monkeypatch.setattr(process.os, "killpg", killpg)
    monkeypatch.setattr(process, "_pid_exists", lambda pid: False)
    process._groups[4242] = _leader(returncode=0)

    await process.stop_all_groups(grace_s=0.1)

    assert signals == []


async def test_ownership_is_rechecked_before_the_kill_pass(monkeypatch, signals):
    # The group is ours when the TERM goes out, then empties and its number is reused
    # inside the grace window: the KILL pass must not follow it there.
    state = {"reused": False}

    def pid_exists(pid: int) -> bool:
        return state["reused"]

    def killpg(pgid: int, sig: int) -> None:
        if sig == signal.SIGTERM:
            state["reused"] = True
        if sig != 0:
            signals.append((pgid, sig))

    monkeypatch.setattr(process, "_pid_exists", pid_exists)
    monkeypatch.setattr(process.os, "killpg", killpg)
    process._groups[4242] = _leader(returncode=0)

    await process.stop_all_groups(grace_s=0.2)

    assert signals == [(4242, signal.SIGTERM)]


async def test_a_backgrounded_child_outliving_its_shell_is_still_stopped():
    proc = await process.spawn_confined(
        "sleep 60 & exit 0",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    pgid = proc.pid
    await proc.wait()  # the shell is reaped; its child lives on in the group
    try:
        assert process._group_is_ours(pgid, proc)

        await process.stop_all_groups(grace_s=2.0)

        for _ in range(100):
            try:
                os.killpg(pgid, 0)
            except ProcessLookupError:
                break
            await asyncio.sleep(0.05)
        else:
            pytest.fail("the backgrounded child survived shutdown")
    finally:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except OSError:
            pass
