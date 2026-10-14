"""A stand-in for :class:`~services.vm.platform.PlatformVM` that needs no VM.

It exposes what the sandbox session and manager use. Commands run on this machine in the
workspace's host directory (the guest copy is the host copy), and every call that crosses
the VM boundary is recorded in :attr:`FakeVM.events`, so a test can assert on ordering
and on what was handed to the guest.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import cast

from services.vm.driver import ExecResult, VMError
from services.vm.egress import EgressGateway
from services.vm.platform import GuestLayout, JobState, PlatformVM
from services.vm.sync import Changes, Manifest


def as_platform(vm: object) -> PlatformVM:
    """The one place a fake is handed to code that wants the concrete VM class."""
    return cast(PlatformVM, vm)


class FakePreview:
    """What :meth:`FakeVM.open_preview` returns: a host port and a close that is recorded."""

    def __init__(self, port: int) -> None:
        self.port = port
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class FakeVM:
    def __init__(self, work_root: Path, *, available: bool = True) -> None:
        self.work_root = work_root
        self.available = available
        self.failure: str | None = None
        self.generation = 1
        self.gateway = EgressGateway()
        self.layout = GuestLayout()
        #: ``(operation, key-or-name)`` in the order the VM was asked.
        self.events: list[tuple[str, str]] = []
        #: Each command's ``(key, argv, env, stdin)``.
        self.runs: list[tuple[str, list[str], dict[str, str], str | None]] = []
        self.pushes: list[tuple[str, Changes]] = []
        self.pulls: list[tuple[str, Changes]] = []
        self.background: dict[str, dict[str, object]] = {}
        self.preview_port = 0
        self.previews: list[FakePreview] = []
        self.alive_after_start = True
        self.log = ""
        #: Raised by the next :meth:`run` and :meth:`push` when set.
        self.fail_with: VMError | None = None
        #: Whether :meth:`run` executes the command on this machine.
        self.execute = True

    async def wait_ready(self, timeout_s: float) -> bool:
        return self.available

    async def run(
        self,
        key: str,
        argv: Sequence[str],
        *,
        env: Mapping[str, str],
        timeout_s: float,
        stdin: str | None = None,
        workdir: str = "",
    ) -> ExecResult:
        self.events.append(("run", key))
        self.runs.append((key, list(argv), dict(env), stdin))
        if self.fail_with is not None:
            raise self.fail_with
        if not self.execute:
            return ExecResult(0, "", "")
        cwd = self.work_root / key
        cwd.mkdir(parents=True, exist_ok=True)
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            env={"PATH": os.environ["PATH"], **env},
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(
                proc.communicate(stdin.encode() if stdin is not None else None), timeout_s
            )
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return ExecResult(124, "", "timed out")
        return ExecResult(proc.returncode or 0, out.decode(), err.decode())

    async def start_job(
        self,
        key: str,
        name: str,
        argv: Sequence[str],
        *,
        env: Mapping[str, str],
        timeout_s: float = 0,
        workdir: str = "",
        publish: int = 0,
        cwd_file: str = "",
    ) -> None:
        self.events.append(("start_job", name))
        if self.fail_with is not None:
            raise self.fail_with
        self.background[name] = {
            "key": key,
            "argv": list(argv),
            "env": dict(env),
            "publish": publish,
            "running": self.alive_after_start,
        }

    async def stop_job(self, name: str) -> None:
        self.events.append(("stop_job", name))
        if name in self.background:
            self.background[name]["running"] = False

    async def job(self, name: str, *, out_offset: int = 0, err_offset: int = 0) -> JobState:
        running = bool(self.background.get(name, {}).get("running"))
        return JobState(
            running=running,
            exit_code=None if running else 1,
            stdout="",
            stderr=self.log,
            out_end=0,
            err_end=len(self.log),
        )

    async def open_preview(self, key: str) -> FakePreview:
        self.events.append(("open_preview", key))
        preview = FakePreview(self.preview_port)
        self.previews.append(preview)
        return preview

    async def manifest(self, key: str) -> Manifest:
        return {}

    async def push(self, key: str, root: Path, changes: Changes) -> None:
        self.events.append(("push", key))
        if self.fail_with is not None:
            raise self.fail_with
        self.pushes.append((key, changes))

    async def pull(self, key: str, root: Path, changes: Changes) -> None:
        self.events.append(("pull", key))
        self.pulls.append((key, changes))

    async def wipe(self, key: str) -> None:
        self.events.append(("wipe", key))

    def restart(self) -> None:
        """The VM was rebuilt: every guest copy is gone."""
        self.generation += 1
