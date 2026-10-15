"""One thread's shell in the platform VM, run in the agent's home.

A command sees the thread's scratch at ``~/scratch`` (and ``/work``) and the projects it
reaches at ``~/projects/<slug>``: the project its working directory is in, and any it names
by path. Each crosses into the VM as it does today, the scratch as the sandbox session's
guest copy and a project as its git-aware copy (``services/vm/project.py``), and what a
command changed in each is brought back after it. A project the thread may not write is
bound read-only, which the guest kernel enforces.

The working directory persists between commands as a home path, and follows a ``cd`` the
command made as long as it lands somewhere the command could see.
"""

from __future__ import annotations

import asyncio
import contextlib
import secrets
import time
from collections import OrderedDict
from collections.abc import Sequence
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Literal

from pydantic_ai.exceptions import ModelRetry
from pydantic_ai_harness._output import truncate_tail

from services.home.backend import Mount
from services.home.paths import HOME, SCRATCH, HomePathError, Place, parse, projects_named
from services.vm import VMError
from services.vm.host import JobHost
from services.vm.platform import HomeBind, JobState
from services.vm.project import BringOut, ProjectCopies, ProjectCopy

from .base import SandboxError
from .jobs import capturing_cwd, follow_job, refuse
from .session import SandboxSession
from .shell_runner import BackgroundStatus, ProgressHook, ShellResult

_TIMEOUT_EXIT = 124
#: How long a stop waits for the job to report it has ended.
_STOP_WAIT_S = 5.0
#: How many projects one command may bring into the VM by naming them.
_MAX_PROJECTS = 8
#: How many finished background commands a later check can still read back.
_MAX_FINISHED = 32


@dataclass(frozen=True, slots=True)
class _Job:
    """A background command, and the copies it holds until it ends."""

    name: str
    session: SandboxSession
    copies: tuple[ProjectCopy, ...]


@dataclass(frozen=True, slots=True)
class HomeResult:
    """A finished command, and what bringing its work back had to say."""

    result: ShellResult
    notes: tuple[str, ...]
    #: Where the next command starts.
    cwd: str


class HomeShell:
    """One thread's commands, over its sandbox session and the project copies."""

    def __init__(
        self, *, default_timeout: float, max_output_chars: int, start: str = SCRATCH.path
    ) -> None:
        self._cwd: str = start
        self._default_timeout: float = default_timeout
        self._max_output_chars: int = max_output_chars
        self._jobs: dict[str, _Job] = {}
        self._finished: OrderedDict[str, BackgroundStatus] = OrderedDict()

    @property
    def cwd(self) -> str:
        return self._cwd

    @property
    def busy(self) -> bool:
        """Whether a background command it started may still be running."""
        return bool(self._jobs)

    def reached(
        self, command: str, mounts: Sequence[Mount], *, network: bool = False
    ) -> list[Mount]:
        """The project mounts a command sees: its working directory's, then each it names,
        up to a cap. Scratch is always there and is not listed.

        A command with the network sees only its working directory's project, so what it
        can send out is what it was started to work on.
        """
        by_name = {m.place.name: m for m in mounts if m.place.kind == "projects"}
        wanted: list[str] = []
        here = _place_of(self._cwd)
        if here is not None and here.kind == "projects":
            wanted.append(here.name)
        if not network:
            wanted += [name for name in projects_named(command) if name not in wanted]
        return [by_name[name] for name in wanted if name in by_name][:_MAX_PROJECTS]

    async def run(
        self,
        session: SandboxSession,
        copies: ProjectCopies,
        command: str,
        *,
        mounts: Sequence[Mount],
        network: bool = False,
        timeout_seconds: float | None = None,
        on_progress: ProgressHook | None = None,
    ) -> HomeResult:
        """Run ``command`` to its end. Cancelled, it stops the job and still brings back
        what the command changed, so nothing keeps writing a copy no lock holds."""
        refuse(command)
        timeout = timeout_seconds if timeout_seconds is not None else self._default_timeout
        projects = self.reached(command, mounts, network=network)
        vm = copies.vm
        name = f"run-{secrets.token_hex(6)}"
        cwd_file = f"{vm.layout.xfer}/{name}.cwd"
        started = time.monotonic()
        held = [(mount, copies.acquire(mount.root)) for mount in projects]
        notes: list[str] = []
        async with AsyncExitStack() as stack:
            _ = await stack.enter_async_context(session.lock)
            # In key order, so two commands reaching the same projects never deadlock.
            for _mount, copy in sorted(held, key=lambda pair: pair[1].workspace.key):
                _ = await stack.enter_async_context(copy.lock)
            try:
                await session.bring_in()
                for _mount, copy in held:
                    await copy.workspace.bring_in()
                await vm.start_job(
                    session.key,
                    name,
                    ["bash", "-c", capturing_cwd(command)],
                    # Read after the bring-in, which is what grants the egress credentials.
                    env=session.command_env(network=network),
                    timeout_s=timeout,
                    workdir=self._cwd,
                    cwd_file=cwd_file,
                    home=_binds(session.key, held),
                )
                try:
                    state = await follow_job(vm, name, started, on_progress)
                except asyncio.CancelledError:
                    await _abandon(vm, name, session, [copy for _, copy in held])
                    raise
                landed = await vm.read_guest(cwd_file, max_bytes=4096)
                await session.bring_out()
                for _mount, copy in held:
                    report: BringOut = await copy.workspace.bring_out()
                    notes += report.notes
            except VMError as exc:
                raise ModelRetry(f"The platform VM could not run the command: {exc}") from exc
            finally:
                for _mount, copy in held:
                    copy.touch()
                with contextlib.suppress(VMError):
                    _ = await vm.call(["rm", "-f", cwd_file])
        timed_out = state.exit_code == _TIMEOUT_EXIT
        exit_code = None if timed_out else state.exit_code
        if exit_code == 0:
            self._follow_cd(landed, [mount.place for mount, _ in held])
        result = ShellResult(
            exit_code=exit_code,
            stdout=truncate_tail(state.stdout, self._max_output_chars),
            stderr=truncate_tail(state.stderr, self._max_output_chars),
            timed_out=timed_out,
            duration_ms=round((time.monotonic() - started) * 1000),
        )
        return HomeResult(result=result, notes=tuple(notes), cwd=self._cwd)

    async def start(
        self,
        session: SandboxSession,
        copies: ProjectCopies,
        command: str,
        *,
        mounts: Sequence[Mount],
        network: bool = False,
    ) -> str:
        """Start ``command`` in the background; its places come back when it ends."""
        refuse(command)
        reached = self.reached(command, mounts, network=network)
        held = [(mount, copies.acquire(mount.root)) for mount in reached]
        command_id = secrets.token_hex(6)
        name = f"bg-{command_id}"
        async with AsyncExitStack() as stack:
            _ = await stack.enter_async_context(session.lock)
            for _mount, copy in sorted(held, key=lambda pair: pair[1].workspace.key):
                _ = await stack.enter_async_context(copy.lock)
            try:
                await session.bring_in()
                for _mount, copy in held:
                    await copy.workspace.bring_in()
                await copies.vm.start_job(
                    session.key,
                    name,
                    ["bash", "-c", command],
                    env=session.command_env(network=network),
                    workdir=self._cwd,
                    home=_binds(session.key, held),
                )
            except VMError as exc:
                raise ModelRetry(f"The platform VM could not start the command: {exc}") from exc
        for _mount, copy in held:
            copy.jobs.add(name)
        self._jobs[command_id] = _Job(name, session, tuple(copy for _, copy in held))
        return command_id

    async def check(self, copies: ProjectCopies, command_id: str) -> BackgroundStatus | None:
        """A background command as it stands. One that has ended is brought back once
        and retired, and later checks read its final status."""
        job = self._jobs.get(command_id)
        if job is None:
            return self._finished.get(command_id)
        state = await copies.vm.job(job.name)
        if state.running:
            return self._status(command_id, state, "running")
        return await self._retire(command_id, job, state, "finished")

    async def stop(self, copies: ProjectCopies, command_id: str) -> BackgroundStatus | None:
        job = self._jobs.get(command_id)
        if job is None:
            return self._finished.get(command_id)
        vm = copies.vm
        await vm.stop_job(job.name)
        deadline = time.monotonic() + _STOP_WAIT_S
        state = await vm.job(job.name)
        while state.running and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
            state = await vm.job(job.name)
        return await self._retire(command_id, job, state, "stopped")

    async def shutdown(self, copies: ProjectCopies) -> None:
        for command_id in list(self._jobs):
            _ = await self.stop(copies, command_id)

    async def _retire(
        self,
        command_id: str,
        job: _Job,
        state: JobState,
        status: Literal["finished", "stopped"],
    ) -> BackgroundStatus:
        _ = self._jobs.pop(command_id, None)
        for copy in job.copies:
            copy.jobs.discard(job.name)
        async with job.session.lock:
            await job.session.bring_out()
        for copy in job.copies:
            async with copy.lock:
                copy.touch()
                _ = await copy.workspace.bring_out()
        final = self._status(command_id, state, status)
        self._finished[command_id] = final
        while len(self._finished) > _MAX_FINISHED:
            _ = self._finished.popitem(last=False)
        return final

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

    def _follow_cd(self, landed: str, bound: Sequence[Place]) -> None:
        """Keep a ``cd`` the command made, when it ended inside something it could see."""
        landed = landed.strip()
        if not landed:
            return
        try:
            target = parse(landed, cwd=self._cwd)
        except HomePathError:
            return
        if target.place == SCRATCH or target.place in bound:
            self._cwd = target.path


async def _abandon(
    vm: JobHost, name: str, session: SandboxSession, copies: Sequence[ProjectCopy]
) -> None:
    """Stop a job whose caller gave up on it, and bring back what it had changed. The
    caller holds the locks."""
    with contextlib.suppress(VMError):
        await vm.stop_job(name)
    with contextlib.suppress(SandboxError):
        await session.bring_out()
    for copy in copies:
        with contextlib.suppress(VMError):
            _ = await copy.workspace.bring_out()


def _binds(scratch_key: str, held: Sequence[tuple[Mount, ProjectCopy]]) -> list[HomeBind]:
    return [
        HomeBind(key=scratch_key, dest="scratch", writable=True),
        *(
            HomeBind(
                key=copy.workspace.key,
                dest=f"projects/{mount.place.name}",
                writable=mount.writable,
            )
            for mount, copy in held
        ),
    ]


def _place_of(path: str) -> Place | None:
    try:
        return parse(path, cwd=HOME + "/scratch").place
    except HomePathError:
        return None
