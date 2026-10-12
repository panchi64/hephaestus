"""The project shell's commands, run in the platform VM against the project's guest copy.

The shape is :class:`~services.sandbox.shell_runner.HostShell`'s — run, start, check,
stop, a working directory that persists between calls — so the model-facing contract in
``tools/shell.py`` is unchanged. What differs is where a command runs: the project folder
is brought into the VM before each command (:class:`~services.vm.project.ProjectWorkspace`)
and what it changed is brought back after, so a command never touches the operator's
machine. A command with no network reaches nothing; one that declared ``network`` gets
the conversation's egress credentials and reaches its allowlist.

The working directory is captured out of band, into a file only the command's own shell
writes, and kept inside the project: the permission judge measures relative paths
against the project root.
"""

from __future__ import annotations

import asyncio
import contextlib
import posixpath
import secrets
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic_ai.exceptions import ModelRetry
from pydantic_ai_harness._output import truncate_tail

from services.vm import VMError
from services.vm.platform import JobState, PlatformVM
from services.vm.project import BringOut, ProjectCopies, ProjectCopy

from .shell_runner import BackgroundStatus, ProgressHook, ShellResult, check_command

_POLL_S = 0.5
_TIMEOUT_EXIT = 124
#: How long a stop waits for the job to report it has ended.
_STOP_WAIT_S = 5.0
_PROGRESS_MAX_CHARS = 200_000


@dataclass(frozen=True, slots=True)
class ProjectResult:
    """A finished command, and what bringing its work out of the VM had to say."""

    result: ShellResult
    notes: tuple[str, ...]


class ProjectShell:
    """One project folder's shell in the VM, shared by the runs working there."""

    def __init__(self, root: Path, *, default_timeout: float, max_output_chars: int) -> None:
        self._root = root
        #: The working directory, relative to the project root ("" is the root).
        self._rel = ""
        self._default_timeout = default_timeout
        self._max_output_chars = max_output_chars
        self._jobs: dict[str, str] = {}

    @property
    def cwd(self) -> Path:
        return self._root / self._rel if self._rel else self._root

    async def run(
        self,
        copies: ProjectCopies,
        command: str,
        *,
        env: Mapping[str, str],
        timeout_seconds: float | None = None,
        on_progress: ProgressHook | None = None,
    ) -> ProjectResult:
        _refuse(command)
        timeout = timeout_seconds if timeout_seconds is not None else self._default_timeout
        copy = copies.acquire(self._root)
        vm = copies.vm
        name = f"run-{secrets.token_hex(6)}"
        cwd_file = f"{vm.layout.xfer}/{name}.cwd"
        started = time.monotonic()
        async with copy.lock:
            try:
                await copy.workspace.bring_in()
                await vm.start_job(
                    copy.workspace.key,
                    name,
                    ["bash", "-c", _capturing_cwd(command)],
                    env=env,
                    timeout_s=timeout,
                    workdir=self._rel,
                    cwd_file=cwd_file,
                )
                state = await self._follow(vm, name, started, on_progress)
                landed = await vm.read_guest(cwd_file, max_bytes=4096)
                report = await copy.workspace.bring_out()
            except VMError as exc:
                raise ModelRetry(f"The platform VM could not run the command: {exc}") from exc
            finally:
                copy.touch()
                with contextlib.suppress(VMError):
                    await vm.call(["rm", "-f", cwd_file])
        timed_out = state.exit_code == _TIMEOUT_EXIT
        exit_code = None if timed_out else state.exit_code
        if exit_code == 0:
            self._apply_cwd(landed)
        result = ShellResult(
            exit_code=exit_code,
            stdout=truncate_tail(state.stdout, self._max_output_chars),
            stderr=truncate_tail(state.stderr, self._max_output_chars),
            timed_out=timed_out,
            duration_ms=round((time.monotonic() - started) * 1000),
        )
        return ProjectResult(result=result, notes=tuple(report.notes))

    async def _follow(
        self,
        vm: PlatformVM,
        name: str,
        started: float,
        on_progress: ProgressHook | None,
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

    async def start(self, copies: ProjectCopies, command: str, *, env: Mapping[str, str]) -> str:
        _refuse(command)
        copy = copies.acquire(self._root)
        command_id = secrets.token_hex(6)
        name = f"bg-{command_id}"
        async with copy.lock:
            try:
                await copy.workspace.bring_in()
                await copies.vm.start_job(
                    copy.workspace.key, name, ["bash", "-c", command], env=env, workdir=self._rel
                )
            except VMError as exc:
                raise ModelRetry(f"The platform VM could not start the command: {exc}") from exc
        copy.jobs.add(name)
        self._jobs[command_id] = name
        return command_id

    async def check(self, copies: ProjectCopies, command_id: str) -> BackgroundStatus | None:
        name = self._jobs.get(command_id)
        if name is None:
            return None
        copy = copies.acquire(self._root)
        state = await copies.vm.job(name)
        if not state.running:
            await self._bring_out(copy)
        return self._status(command_id, state, "running" if state.running else "finished")

    async def stop(self, copies: ProjectCopies, command_id: str) -> BackgroundStatus | None:
        name = self._jobs.pop(command_id, None)
        if name is None:
            return None
        copy = copies.acquire(self._root)
        vm = copies.vm
        await vm.stop_job(name)
        deadline = time.monotonic() + _STOP_WAIT_S
        state = await vm.job(name)
        while state.running and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
            state = await vm.job(name)
        copy.jobs.discard(name)
        await self._bring_out(copy)
        return self._status(command_id, state, "stopped")

    async def shutdown(self, copies: ProjectCopies) -> None:
        for command_id in list(self._jobs):
            await self.stop(copies, command_id)

    async def _bring_out(self, copy: ProjectCopy) -> BringOut:
        async with copy.lock:
            copy.touch()
            return await copy.workspace.bring_out()

    def _status(
        self,
        command_id: str,
        state: JobState,
        status: Literal["running", "finished", "stopped"],
    ) -> BackgroundStatus:
        return BackgroundStatus(
            command_id=command_id,
            status=status,
            exit_code=state.exit_code,
            stdout=truncate_tail(state.stdout, self._max_output_chars),
            stderr=truncate_tail(state.stderr, self._max_output_chars),
        )

    def _apply_cwd(self, landed: str) -> None:
        """Follow a `cd` the command made, while it stays inside the project."""
        landed = landed.strip()
        if landed == "/work":
            self._rel = ""
        elif landed.startswith("/work/"):
            rel = posixpath.normpath(landed[len("/work/") :])
            if rel != ".." and not rel.startswith("../"):
                self._rel = rel


def _refuse(command: str) -> None:
    """The shell's own refusals, as something the model can act on."""
    try:
        check_command(command)
    except PermissionError as exc:
        raise ModelRetry(str(exc)) from exc


def _capturing_cwd(command: str) -> str:
    return f'{command}\n__ody_ec=$?\npwd > /run/cwd\nexit "$__ody_ec"'
