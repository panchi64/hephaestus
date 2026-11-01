"""How `run_on_host` ends a command: on a timeout, on a cancel, and when it leaves a child."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from services.sandbox import run_on_host


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def _gone(pid: int, within_s: float = 3.0) -> bool:
    deadline = asyncio.get_running_loop().time() + within_s
    while asyncio.get_running_loop().time() < deadline:
        if not _alive(pid):
            return True
        await asyncio.sleep(0.05)
    return not _alive(pid)


async def test_a_timeout_reports_124_with_what_the_command_printed() -> None:
    result = await run_on_host("echo partial; echo warn >&2; sleep 30", timeout_s=0.5)

    assert result.timed_out and result.exit_code == 124
    assert result.stdout.strip() == "partial"
    assert result.stderr.strip() == "warn"


async def test_a_cancel_during_the_grace_period_kills_a_command_that_ignores_term(
    tmp_path: Path,
) -> None:
    pid_file = tmp_path / "pid"
    command = f"trap '' TERM; echo $$ > {pid_file}; sleep 30"
    task = asyncio.ensure_future(run_on_host(command, timeout_s=0.3))
    await asyncio.sleep(0.8)  # timed out, and inside the grace window for SIGTERM
    pid = int(pid_file.read_text())
    assert _alive(pid)

    _ = task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert await _gone(pid)


async def test_a_child_the_command_backgrounds_does_not_outlive_the_call() -> None:
    result = await run_on_host("sleep 30 & echo $!", timeout_s=10)

    child = int(result.stdout.strip())
    assert result.exit_code == 0
    assert await _gone(child)
