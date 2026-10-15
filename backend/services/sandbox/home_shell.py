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

import contextlib
import re
import secrets
import time
from collections.abc import Mapping, Sequence
from contextlib import AsyncExitStack
from dataclasses import dataclass

from pydantic_ai.exceptions import ModelRetry
from pydantic_ai_harness._output import truncate_tail

from services.home.backend import Mount
from services.home.paths import HOME, SCRATCH, HomePathError, Place, parse
from services.vm import VMError
from services.vm.platform import HomeBind
from services.vm.project import BringOut, ProjectCopies, ProjectCopy

from .jobs import capturing_cwd, follow_job, refuse
from .session import SandboxSession
from .shell_runner import ProgressHook, ShellResult

_TIMEOUT_EXIT = 124
#: How many projects one command may bring into the VM by naming them.
_MAX_PROJECTS = 8
_NAMED_PROJECT = re.compile(r"(?:~|/home/agent)/projects/([a-z0-9][a-z0-9._-]{0,79})")


@dataclass(frozen=True, slots=True)
class HomeResult:
    """A finished command, and what bringing its work back had to say."""

    result: ShellResult
    notes: tuple[str, ...]
    #: Where the next command starts.
    cwd: str


class HomeShell:
    """One thread's commands, over its sandbox session and the project copies."""

    def __init__(self, *, default_timeout: float, max_output_chars: int) -> None:
        self._cwd: str = SCRATCH.path
        self._default_timeout: float = default_timeout
        self._max_output_chars: int = max_output_chars

    @property
    def cwd(self) -> str:
        return self._cwd

    def reached(self, command: str, mounts: Sequence[Mount]) -> list[Mount]:
        """The project mounts a command sees: its working directory's, then each it names,
        up to a cap. Scratch is always there and is not listed."""
        by_name = {m.place.name: m for m in mounts if m.place.kind == "projects"}
        wanted: list[str] = []
        here = _place_of(self._cwd)
        if here is not None and here.kind == "projects":
            wanted.append(here.name)
        for match in _NAMED_PROJECT.finditer(command):
            name: str = match.group(1)
            if name not in wanted:
                wanted.append(name)
        return [by_name[name] for name in wanted if name in by_name][:_MAX_PROJECTS]

    async def run(
        self,
        session: SandboxSession,
        copies: ProjectCopies,
        command: str,
        *,
        mounts: Sequence[Mount],
        env: Mapping[str, str],
        timeout_seconds: float | None = None,
        on_progress: ProgressHook | None = None,
    ) -> HomeResult:
        refuse(command)
        timeout = timeout_seconds if timeout_seconds is not None else self._default_timeout
        projects = self.reached(command, mounts)
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
                    env=env,
                    timeout_s=timeout,
                    workdir=self._cwd,
                    cwd_file=cwd_file,
                    home=_binds(session.key, held),
                )
                state = await follow_job(vm, name, started, on_progress)
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

    def change_dir(self, path: str, mounts: Sequence[Mount]) -> str:
        """Move the working directory to ``path``, a folder in a place the thread has."""
        target = parse(path, cwd=self._cwd)
        if target.place != SCRATCH and target.place not in {m.place for m in mounts}:
            raise HomePathError(
                f"there is no {target.place.path} in your home", hint="list ~/projects"
            )
        self._cwd = target.path
        return self._cwd

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
