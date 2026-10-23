"""What a host command hands back, and what it says while it is still running.

A result is structure, not a labelled string: exit code, the two streams apart, whether the
command was killed on its timeout, and how long it took. These tests pin that structure,
including a timed-out command's output and output that arrives before the command is over.

The runner is driven directly here, with no profile and no toolset. ``tools/shell.py`` owns
where a command runs and ``tests/test_shell_fence.py`` and ``tests/test_project_shell.py``
test the routes; what is under test here is the shape of the answer.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from services.sandbox import streams
from services.sandbox.host_shell import HostShell


def _shell(root: Path, *, timeout: float = 30.0, max_chars: int = 1_000_000) -> HostShell:
    root.mkdir(parents=True, exist_ok=True)
    return HostShell(
        root,
        default_timeout=timeout,
        max_output_chars=max_chars,
    )


async def test_the_two_streams_come_back_apart(tmp_path: Path):
    result = await _shell(tmp_path / "w").run(
        "echo out; echo err >&2",
    )
    assert result.stdout.strip() == "out"
    assert result.stderr.strip() == "err"
    assert result.exit_code == 0 and result.ok and not result.timed_out


async def test_a_silent_command_is_two_empty_strings_and_not_a_sentence(tmp_path: Path):
    result = await _shell(tmp_path / "w").run("true")
    assert result.stdout == "" and result.stderr == ""
    assert result.ok


async def test_a_failing_command_carries_its_status_as_a_number(tmp_path: Path):
    result = await _shell(tmp_path / "w").run("echo nope >&2; exit 3")
    assert result.exit_code == 3 and not result.ok
    assert "nope" in result.stderr


async def test_a_command_is_timed_and_the_duration_is_its_own(tmp_path: Path):
    result = await _shell(tmp_path / "w").run("sleep 0.3")
    assert result.duration_ms >= 250


async def test_a_timed_out_command_keeps_what_it_printed(tmp_path: Path):
    result = await _shell(tmp_path / "w").run("echo reached-here; sleep 5", timeout_seconds=0.4)
    assert result.timed_out is True
    assert "reached-here" in result.stdout
    # None, not 0: no status was collected.
    assert result.exit_code is None and not result.ok


async def test_a_timed_out_command_does_not_move_the_working_directory(tmp_path: Path):
    # The directory is only adopted from a command that exited cleanly.
    shell = _shell(tmp_path / "w")
    (tmp_path / "w" / "sub").mkdir()
    before = shell.cwd
    _ = await shell.run("cd sub; sleep 5", timeout_seconds=0.3)
    assert shell.cwd == before


async def test_output_arrives_while_the_command_is_still_running(tmp_path: Path):
    chunks: list[tuple[str, float]] = []

    async def hook(chunk: str, elapsed_s: float) -> None:
        chunks.append((chunk, elapsed_s))

    result = await _shell(tmp_path / "w").run(
        "echo first; sleep 1.2; echo second", on_progress=hook
    )
    # More than one chunk shows the first landed while the command was still sleeping.
    assert len(chunks) >= 2
    assert "first" in chunks[0][0] and "second" not in chunks[0][0]
    assert "second" in "".join(chunk for chunk, _ in chunks)
    assert chunks[0][1] > 0 and chunks[-1][1] >= chunks[0][1]
    assert "first" in result.stdout and "second" in result.stdout


async def test_the_last_thing_a_command_printed_is_not_lost_to_the_polling_interval(
    tmp_path: Path,
):
    # The pump stops on a signal, not a cancellation, so it gets one more pass after exit.
    chunks: list[str] = []

    async def hook(chunk: str, _elapsed: float) -> None:
        chunks.append(chunk)

    _ = await _shell(tmp_path / "w").run("echo quick", on_progress=hook)
    assert "quick" in "".join(chunks)


async def test_a_progress_hook_that_fails_does_not_fail_the_command(tmp_path: Path):
    async def hook(_chunk: str, _elapsed: float) -> None:
        raise RuntimeError("the stream went away")

    result = await _shell(tmp_path / "w").run("echo fine", on_progress=hook)
    assert result.ok and "fine" in result.stdout


async def test_streaming_stops_at_its_budget_and_the_result_still_has_everything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # The live stream is held in memory and replayed on reattach, so it is capped; the
    # result is not.
    monkeypatch.setattr(streams, "PROGRESS_MAX_CHARS", 4)
    chunks: list[str] = []

    async def hook(chunk: str, _elapsed: float) -> None:
        chunks.append(chunk)

    result = await _shell(tmp_path / "w").run(
        "echo aaaaaaaaaa; sleep 1.2; echo bbbbbbbbbb", on_progress=hook
    )
    assert len(chunks) == 1 and "bbbb" not in chunks[0]
    assert "aaaaaaaaaa" in result.stdout and "bbbbbbbbbb" in result.stdout


async def test_a_cancelled_command_does_not_leave_its_watcher_running(tmp_path: Path):
    ticks: list[str] = []

    async def hook(chunk: str, _elapsed: float) -> None:
        ticks.append(chunk)

    shell = _shell(tmp_path / "w")
    task = asyncio.ensure_future(shell.run("echo up; sleep 5", on_progress=hook))
    await asyncio.sleep(0.7)  # past the first tick
    _ = task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    seen = len(ticks)
    await asyncio.sleep(1.2)  # a surviving pump would tick again
    assert len(ticks) == seen


async def test_a_background_command_reports_status_and_streams_as_fields(tmp_path: Path):
    shell = _shell(tmp_path / "w")
    command_id = await shell.start("echo up; sleep 30")
    status = await shell.check(command_id)
    for _ in range(50):
        assert status is not None
        if "up" in status.stdout:
            break
        await asyncio.sleep(0.05)
        status = await shell.check(command_id)
    assert status is not None
    assert status.status == "running" and status.exit_code is None
    assert status.command_id == command_id

    stopped = await shell.stop(command_id)
    assert stopped is not None and stopped.status == "stopped"


async def test_an_unknown_background_id_is_none_rather_than_a_sentence(tmp_path: Path):
    shell = _shell(tmp_path / "w")
    assert await shell.check("nope") is None
    assert await shell.stop("nope") is None
