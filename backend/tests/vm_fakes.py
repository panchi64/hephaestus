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
from typing import final, override

from services.vm.driver import ExecResult, VMError
from services.vm.egress import EgressGateway
from services.vm.host import ProjectHost, SessionHost
from services.vm.platform import GuestLayout, HomeBind, JobState
from services.vm.sync import Changes, Manifest


@final
class FakePreview:
    """What :meth:`FakeVM.open_preview` returns: a host port and a close that is recorded."""

    def __init__(self, port: int) -> None:
        self.port: int = port
        self.closed: bool = False

    async def close(self) -> None:
        self.closed = True


@final
class FakeVM(SessionHost, ProjectHost):
    def __init__(self, work_root: Path, *, available: bool = True) -> None:
        self.work_root: Path = work_root
        self._available: bool = available
        self._failure: str | None = None
        self._generation: int = 1
        self._gateway: EgressGateway = EgressGateway()
        self._layout: GuestLayout = GuestLayout()
        #: ``(operation, key-or-name)`` in the order the VM was asked.
        self.events: list[tuple[str, str]] = []
        #: Each command's ``(key, argv, env, stdin)``.
        self.runs: list[tuple[str, list[str], dict[str, str], str | None]] = []
        #: Each command's home binds, run and background alike, in order.
        self.homes: list[tuple[HomeBind, ...]] = []
        #: Guest files :meth:`read_guest` answers with, by guest path.
        self.guest_files: dict[str, str] = {}
        self.pushes: list[tuple[str, Changes]] = []
        self.pulls: list[tuple[str, Changes]] = []
        self.background: dict[str, dict[str, object]] = {}
        self.preview_port: int = 0
        self.previews: list[FakePreview] = []
        self.alive_after_start: bool = True
        self.log: str = ""
        #: Raised by the next :meth:`run` and :meth:`push` when set.
        self.fail_with: VMError | None = None
        #: Whether :meth:`run` executes the command on this machine.
        self.execute: bool = True

    @property
    @override
    def available(self) -> bool:
        return self._available

    @available.setter
    def available(self, value: bool) -> None:
        self._available = value

    @property
    @override
    def failure(self) -> str | None:
        return self._failure

    @failure.setter
    def failure(self, value: str | None) -> None:
        self._failure = value

    @property
    @override
    def generation(self) -> int:
        return self._generation

    @property
    @override
    def gateway(self) -> EgressGateway:
        return self._gateway

    @property
    @override
    def layout(self) -> GuestLayout:
        return self._layout

    @override
    async def wait_ready(self, timeout_s: float) -> bool:
        return self.available

    @override
    async def run(
        self,
        key: str,
        argv: Sequence[str],
        *,
        env: Mapping[str, str],
        timeout_s: float,
        stdin: str | None = None,
        workdir: str = "",
        home: Sequence[HomeBind] = (),
    ) -> ExecResult:
        self.events.append(("run", key))
        self.homes.append(tuple(home))
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
            _ = await proc.wait()
            return ExecResult(124, "", "timed out")
        return ExecResult(proc.returncode or 0, out.decode(), err.decode())

    @override
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
        home: Sequence[HomeBind] = (),
    ) -> None:
        self.events.append(("start_job", name))
        self.homes.append(tuple(home))
        if self.fail_with is not None:
            raise self.fail_with
        self.background[name] = {
            "key": key,
            "argv": list(argv),
            "env": dict(env),
            "publish": publish,
            "workdir": workdir,
            "running": self.alive_after_start,
        }

    @override
    async def stop_job(self, name: str) -> None:
        self.events.append(("stop_job", name))
        if name in self.background:
            self.background[name]["running"] = False

    @override
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

    @override
    async def open_preview(self, key: str) -> FakePreview:
        self.events.append(("open_preview", key))
        preview = FakePreview(self.preview_port)
        self.previews.append(preview)
        return preview

    @override
    async def manifest(self, key: str) -> Manifest:
        return {}

    @override
    async def push(self, key: str, root: Path, changes: Changes) -> None:
        self.events.append(("push", key))
        if self.fail_with is not None:
            raise self.fail_with
        self.pushes.append((key, changes))

    @override
    async def pull(self, key: str, root: Path, changes: Changes, *, project: bool = False) -> None:
        self.events.append(("pull", key))
        self.pulls.append((key, changes))

    @override
    def scratch_path(self, suffix: str = "") -> Path:
        raise AssertionError("unexpected call: scratch_path")

    @override
    async def call(self, argv: list[str], *, timeout_s: float = 120.0) -> ExecResult:
        if argv[:2] == ["rm", "-f"]:
            self.events.append(("rm", " ".join(argv[2:])))
            return ExecResult(0, "", "")
        raise AssertionError(f"unexpected call: call {argv}")

    @override
    async def call_checked(self, argv: list[str], *, what: str) -> str:
        raise AssertionError(f"unexpected call: call_checked {argv}")

    @override
    async def put_path(self, local: Path) -> str:
        raise AssertionError(f"unexpected call: put_path {local}")

    @override
    async def fetch_file(self, guest: str, local: Path, *, max_bytes: int) -> None:
        raise AssertionError(f"unexpected call: fetch_file {guest}")

    @override
    async def read_guest(self, guest: str, *, max_bytes: int) -> str:
        """What a command left in a guest file (:attr:`guest_files`), or "" as if missing."""
        return self.guest_files.get(guest, "")[:max_bytes]

    @override
    async def wipe(self, key: str) -> None:
        self.events.append(("wipe", key))

    def restart(self) -> None:
        """The VM was rebuilt: every guest copy is gone."""
        self._generation += 1
