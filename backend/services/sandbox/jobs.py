"""Following one shell command run as a VM job: its output as it arrives, and where it
left the working directory."""

from __future__ import annotations

import asyncio
import contextlib
import time

from pydantic_ai.exceptions import ModelRetry

from services.vm.host import JobHost
from services.vm.platform import JobState

from .shell_runner import ProgressHook, check_command

_POLL_S = 0.5
_PROGRESS_MAX_CHARS = 200_000


async def follow_job(
    vm: JobHost, name: str, started: float, on_progress: ProgressHook | None
) -> JobState:
    """Poll a job to its end, streaming what it prints, and return all of it."""
    out: list[str] = []
    err: list[str] = []
    out_at = err_at = 0
    streamed = 0
    while True:
        state = await vm.job(name, out_offset=out_at, err_offset=err_at)
        out.append(state.stdout)
        err.append(state.stderr)
        out_at, err_at = state.out_end, state.err_end
        chunk = state.stdout + state.stderr
        if on_progress is not None and chunk and streamed < _PROGRESS_MAX_CHARS:
            with contextlib.suppress(Exception):
                await on_progress(
                    chunk[: _PROGRESS_MAX_CHARS - streamed], time.monotonic() - started
                )
            streamed += len(chunk)
        if not state.running:
            return JobState(
                running=False,
                exit_code=state.exit_code,
                stdout="".join(out),
                stderr="".join(err),
                out_end=out_at,
                err_end=err_at,
            )
        await asyncio.sleep(_POLL_S)


def refuse(command: str) -> None:
    """The shell's own refusals, as something the model can act on."""
    try:
        check_command(command)
    except PermissionError as exc:
        raise ModelRetry(str(exc)) from exc


def capturing_cwd(command: str) -> str:
    """``command`` followed by writing its final working directory to ``/run/cwd``, keeping
    its own exit status."""
    return f'{command}\n__heph_ec=$?\npwd > /run/cwd\nexit "$__heph_ec"'
