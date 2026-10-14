"""The slices of the platform VM each consumer reaches, so a fake can stand in for it."""

from __future__ import annotations

from collections.abc import Callable, Coroutine, Mapping, Sequence
from pathlib import Path
from typing import Protocol

from .driver import ExecResult
from .egress import EgressGateway
from .platform import GuestLayout, JobState
from .sync import Changes, Manifest


class PreviewForward(Protocol):
    """A host loopback port reaching a workspace's preview, closed when the preview ends."""

    @property
    def port(self) -> int: ...

    async def close(self) -> None: ...


class WorkspaceHost(Protocol):
    """What a guest copy of a workspace needs to be pushed in, pulled back and wiped."""

    @property
    def generation(self) -> int: ...

    @property
    def available(self) -> bool: ...

    async def wipe(self, key: str) -> None: ...

    async def push(self, key: str, root: Path, changes: Changes) -> None: ...

    async def pull(
        self, key: str, root: Path, changes: Changes, *, project: bool = False
    ) -> None: ...

    async def manifest(self, key: str) -> Manifest: ...


class JobHost(WorkspaceHost, Protocol):
    """What a shell command's background job and its egress grants go through."""

    @property
    def failure(self) -> str | None: ...

    @property
    def gateway(self) -> EgressGateway: ...

    async def wait_ready(self, timeout_s: float) -> bool: ...

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
    ) -> None: ...

    async def stop_job(self, name: str) -> None: ...

    async def job(self, name: str, *, out_offset: int = 0, err_offset: int = 0) -> JobState: ...


class SessionHost(JobHost, Protocol):
    """What a sandbox session runs commands and previews through."""

    async def run(
        self,
        key: str,
        argv: Sequence[str],
        *,
        env: Mapping[str, str],
        timeout_s: float,
        stdin: str | None = None,
        workdir: str = "",
    ) -> ExecResult: ...

    async def open_preview(self, key: str) -> PreviewForward: ...


class ProjectHost(JobHost, Protocol):
    """What a project folder's guest copy needs beyond a plain workspace: git transfers."""

    @property
    def layout(self) -> GuestLayout: ...

    def scratch_path(self, suffix: str = "") -> Path: ...

    async def call(self, argv: list[str], *, timeout_s: float = ...) -> ExecResult: ...

    async def call_checked(self, argv: list[str], *, what: str) -> str: ...

    async def put_path(self, local: Path) -> str: ...

    async def fetch_file(self, guest: str, local: Path, *, max_bytes: int) -> None: ...

    async def read_guest(self, guest: str, *, max_bytes: int) -> str: ...


class ServiceHost(Protocol):
    """What a managed platform service uses to start, forward and restart itself."""

    @property
    def layout(self) -> GuestLayout: ...

    def on_ready(self, hook: Callable[[], Coroutine[object, object, None]]) -> None: ...

    async def wait_ready(self, timeout_s: float) -> bool: ...

    async def put_text(self, guest: str, text: str, *, mode: str = "0644") -> None: ...

    async def start_service(self, name: str, user: str, argv: Sequence[str]) -> None: ...

    async def stop_service(self, name: str) -> None: ...

    async def forward(self, guest_port: int) -> int: ...
