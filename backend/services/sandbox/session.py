"""One conversation's live sandbox: its workspace and the guest copy commands run in.

A conversation gets one workspace, a host-side directory holding the agent's files for as
long as the conversation exists. Commands run in the platform VM (:mod:`services.vm`)
against a guest copy that is piped in before a command and back out after it. Nothing is
created until the agent first runs code, and reaping an idle session wipes only the guest
copy: a `.venv`, a `dist/` or a cloned `.git` live in the host directory and come back with
the next command.

Each command is confined to its own workspace inside the VM and reaches the network only
through the egress gateway with this session's credentials, which hold it to the
conversation's allowlist.

When a session is created and reaped is policy over the whole set, in
:mod:`services.sandbox.manager`.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import threading
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Protocol

from core import net
from core.vault import Vault
from services.vm import GuestWorkspace, VMError
from services.vm.host import SessionHost
from services.vm.platform import WORKSPACE_PROXY

from .base import SandboxError, SandboxResult, SandboxSpec, contained_path
from .legacy_seal import adopt_legacy_archive, partial_marker, retire_superseded_archive
from .limits import TIMEOUT_EXIT, VM_WAIT_S
from .preview import PreviewHandle
from .walk import walk_files

logger = logging.getLogger(__name__)

#: Workspace-relative dirs the env defaults point at, created before a run so the first
#: command already finds them. Scratch, and excluded from what a walk reports.
_TMP_SUBDIR = ".tmp"
_HOME_SUBDIR = ".home"
#: Package-manager env so installs land in the workspace rather than the read-only system:
#: pip's ``--user`` target and caches, the build temp, and a writable ``HOME``.
_ENV_DEFAULTS = {
    "HOME": f"/work/{_HOME_SUBDIR}",
    "PIP_USER": "1",
    # The system Python is the distribution's, which refuses user installs unless told;
    # the system itself is read-only to the sandbox user either way.
    "PIP_BREAK_SYSTEM_PACKAGES": "1",
    "PYTHONUSERBASE": "/work/.local",
    "PIP_CACHE_DIR": "/work/.cache/pip",
    "PIP_DISABLE_PIP_VERSION_CHECK": "1",
    "TMPDIR": f"/work/{_TMP_SUBDIR}",
}


def proxy_env(credentials: str | None) -> dict[str, str]:
    """Both spellings, because curl reads only the lowercase names while pip, uv, npm,
    cargo, go and git read the uppercase pair.

    Without credentials the gateway answers every request with a 407, which a client
    reports as a proxy refusal rather than a DNS failure."""
    url = f"http://{credentials}@{WORKSPACE_PROXY}" if credentials else f"http://{WORKSPACE_PROXY}"
    direct = "localhost,127.0.0.1"
    return {
        "HTTP_PROXY": url,
        "HTTPS_PROXY": url,
        "http_proxy": url,
        "https_proxy": url,
        "NO_PROXY": direct,
        "no_proxy": direct,
    }


class LiveWork(Protocol):
    """Just enough of a ``runs.Run`` for a session to know whether the work that claimed
    it is still going.

    A claim is released by asking, never by a hand-back: the caller owing the hand-back is
    the one that gets cancelled or dies, and a leaked claim pins a guest copy for the life
    of the process.
    """

    @property
    def is_terminal(self) -> bool: ...


class SandboxSession:
    """One conversation's workspace plus its guest copy in the platform VM."""

    def __init__(
        self,
        key: str,
        *,
        workspace: Path,
        sealed: Path,
        egress_dir: Path,
        vm: SessionHost,
        vault: Vault,
        excludes: Iterable[str],
    ) -> None:
        self.key: str = key
        self.workspace: Path = workspace
        #: Where a legacy sealed archive for this key would be; read once and retired (see
        #: :mod:`services.sandbox.legacy_seal`).
        self.sealed: Path = sealed
        # Owned by the egress policy, which rewrites it on each approval; the gateway
        # re-reads it, so a grant lands on the next request.
        self._egress_dir: Path = egress_dir
        self._vm: SessionHost = vm
        self._guest: GuestWorkspace = GuestWorkspace(vm, key, workspace)
        self._credentials: str | None = None
        self._preview_name: str = f"preview-{key}"
        self._vault: Vault = vault
        # Reporting only, never deletion (:mod:`services.sandbox.walk`).
        self._walk_excludes: tuple[str, ...] = tuple(excludes)
        self._preview: PreviewHandle | None = None
        self._last_used: float = time.monotonic()
        self._lock: asyncio.Lock = asyncio.Lock()
        # Orders `_ensure_workspace` (which may adopt a legacy archive) against the file
        # tools, which take no session lock. A thread lock because the sides are
        # synchronous and some run off the loop.
        self._disk: threading.RLock = threading.RLock()
        self._holders: list[LiveWork] = []

    @property
    def is_busy(self) -> bool:
        """A call (exec, preview launch or teardown) is in flight right now. This is the
        lock, which spans one call and not the turn; see :attr:`is_claimed`."""
        return self._lock.locked()

    def hold(self, holder: LiveWork | None) -> None:
        """Claim this session for a unit of work until it reaches a terminal state.
        Idempotent. ``None`` claims nothing (a caller with no lifetime to tie a guest copy
        to) but still prunes finished claims."""
        self._holders = [h for h in self._holders if not h.is_terminal and h is not holder]
        if holder is not None:
            self._holders.append(holder)

    @property
    def is_claimed(self) -> bool:
        """Whether a run that has not finished is still working in here.

        Covers the gap the lock cannot: between two execs of one turn the session looks
        idle. Pruned on read because the run that would release is the one being cancelled."""
        self._holders = [h for h in self._holders if not h.is_terminal]
        return bool(self._holders)

    @property
    def is_displaceable(self) -> bool:
        """Whether displacing this session at the live-session cap would take it from
        somebody. It would if:

        - a call is in flight, since wiping the guest copy mid-exec fails the tool call;
        - a run is between tool calls, whose files would survive but not its live
          processes and shell state;
        - a preview is live, since reaping drops the proxy's token and 404s the page.

        The idle sweep asks a different question (busy, plus the TTL): a stale preview or a
        run parked on an unanswered approval should be collected eventually.
        """
        return not self.is_busy and not self.is_claimed and self._preview is None

    @property
    def is_warm(self) -> bool:
        """True when the guest already holds this workspace; False before the first run or
        after a reap, when ``run()`` must first pipe it in."""
        return self._guest.is_in

    @property
    def preview(self) -> PreviewHandle | None:
        return self._preview

    @preview.setter
    def preview(self, handle: PreviewHandle | None) -> None:
        self._preview = handle

    @property
    def last_used(self) -> float:
        """The monotonic time of the last call on this session."""
        return self._last_used

    @last_used.setter
    def last_used(self, value: float) -> None:
        self._last_used = value

    @property
    def lock(self) -> asyncio.Lock:
        """The lock that spans one call; held while a call is in flight."""
        return self._lock

    @property
    def credentials(self) -> str | None:
        """The egress proxy credentials granted to this session, or ``None`` when it holds none."""
        return self._credentials

    @property
    def egress_dir(self) -> Path:
        """This workspace's allowlist directory."""
        return self._egress_dir

    def touch(self) -> None:
        self._last_used = time.monotonic()

    def idle_seconds(self, now: float) -> float:
        return now - self._last_used

    async def run(self, spec: SandboxSpec) -> SandboxResult:
        async with self._lock:
            self.touch()
            try:
                return await self._run_inner(spec)
            except SandboxError:
                raise
            except Exception as exc:  # surface as a failure, never crash the agent
                raise SandboxError(f"unexpected sandbox failure: {exc}") from exc

    async def _run_inner(self, spec: SandboxSpec) -> SandboxResult:
        self._ensure_workspace()
        await self._ensure_up()
        try:
            result = await self._vm.run(
                self.key,
                list(spec.command),
                env={**_ENV_DEFAULTS, **self._network_env(), **spec.env},
                timeout_s=spec.timeout_s,
                stdin=spec.stdin,
            )
            lost = await self._guest.bring_out()
        except VMError as exc:
            raise SandboxError(str(exc)) from exc
        return SandboxResult(
            exit_code=result.exit_code,
            stdout=result.stdout,
            stderr=result.stderr,
            timed_out=result.exit_code == TIMEOUT_EXIT,
            notes=(lost,) if lost is not None else (),
        )

    def _network_env(self) -> dict[str, str]:
        return proxy_env(self._credentials) if self._credentials else {}

    async def bring_in(self) -> None:
        """Bring this workspace into the VM, with its egress fence in place, for a command
        another runner starts there under :attr:`key`. The caller holds :attr:`lock`."""
        self.touch()
        self._ensure_workspace()
        await self._ensure_up()

    async def bring_out(self) -> str | None:
        """Bring back what a command changed in the guest copy, returning a note when the
        copy was lost to a VM restart. The caller holds :attr:`lock`."""
        try:
            return await self._guest.bring_out()
        except VMError as exc:
            raise SandboxError(f"could not bring the work back from the VM: {exc}") from exc

    def command_env(self, *, network: bool) -> dict[str, str]:
        """The environment a home command runs with: the package-manager defaults and the
        egress proxy, with credentials only when it may reach the network. ``HOME`` is left
        to the confinement so ``~/projects`` resolves."""
        defaults = {name: value for name, value in _ENV_DEFAULTS.items() if name != "HOME"}
        return {**defaults, **(self._network_env() if network else proxy_env(None))}

    def read_file(self, relpath: str) -> bytes:
        """Read a file the agent produced in this session's workspace, whether or not
        the guest holds it. Guards against escape."""
        self._ensure_workspace()
        target = contained_path(self.workspace, relpath)
        if not target.is_file():
            raise SandboxError(f"no such file in the sandbox: {relpath!r}")
        return target.read_bytes()

    def write_file(self, relpath: str, content: bytes) -> None:
        """Stage a file into this session's workspace, whether or not the guest holds it.
        Writes the host copy, which the next run pushes in. Guards against escape."""
        self._ensure_workspace()
        target = contained_path(self.workspace, relpath)
        target.parent.mkdir(parents=True, exist_ok=True)
        _ = target.write_bytes(content)
        self.touch()

    async def start_preview(
        self, command: list[str], port: int, *, token: str, startup_timeout_s: float
    ) -> PreviewHandle:
        """Run ``command`` as a live server over this workspace, reachable on a
        loopback host port. Replaces any preview already running here (one per
        conversation). Raises :class:`SandboxError` if the server never serves."""
        async with self._lock:
            self.touch()
            self._ensure_workspace()
            await self._ensure_up()
            await self._stop_preview_locked()
            try:
                await self._vm.start_job(
                    self.key,
                    self._preview_name,
                    command,
                    env={**_ENV_DEFAULTS, **self._network_env()},
                    publish=port,
                )
                forward = await self._vm.open_preview(self.key)
            except VMError as exc:
                raise SandboxError(f"failed to start preview server: {exc}") from exc
            handle = PreviewHandle(
                token=token,
                host_port=forward.port,
                server_port=port,
                command=tuple(command),
                forward=forward,
            )
            # The iframe fetches the instant `view.live` is emitted, so wait for a real
            # response. A merely slow server is still opened; an exited one is not.
            serving = await net.await_http_ready(
                f"http://127.0.0.1:{forward.port}/", startup_timeout_s
            )
            state = None if serving else await self._vm.job(self._preview_name)
            if state is not None and not state.running:
                await forward.close()
                log = (state.stdout + state.stderr)[-4000:].strip()
                detail = "the preview server exited without serving a request"
                raise SandboxError(detail + (f"\n--- server logs ---\n{log}" if log else ""))
            self._preview = handle
            return handle

    async def stop_preview(self) -> None:
        """Tear down this session's preview server, if any."""
        async with self._lock:
            await self._stop_preview_locked()

    async def _stop_preview_locked(self) -> None:
        preview, self._preview = self._preview, None
        if preview is None:
            return
        if preview.forward is not None:
            await preview.forward.close()
        try:
            await self._vm.stop_job(self._preview_name)
        except VMError:
            logger.info("sandbox %s: the preview server could not be stopped", self.key)

    async def shutdown(self) -> None:
        """Bring the work out of the guest and wipe the guest copy, leaving the workspace
        on disk. This is the whole of a reap: it reclaims the guest copy, its processes
        and its preview, not the files."""
        async with self._lock:
            await self._release()

    async def discard(self) -> None:
        """Bring the work out and release the guest copy, run when a conversation is being
        deleted. Disk cleanup has a single home (``SandboxSessionManager._purge_disk``)."""
        async with self._lock:
            await self._release()

    async def _release(self) -> None:
        await self._stop_preview_locked()
        try:
            await self._guest.evict()
        except VMError:
            # The guest copy goes with the VM if unreachable; the host copy is untouched.
            logger.warning("sandbox %s: could not bring the guest copy out", self.key)
            self._guest.forget()
        if self._credentials is not None:
            self._vm.gateway.revoke(self._credentials)
            self._credentials = None

    def collect_text_files(
        self, *, max_file_bytes: int = 262_144, max_files: int = 2000
    ) -> dict[str, bytes]:
        """The workspace's text files (relpath → bytes) for a history snapshot: the files
        every other walk reports, minus those over ``max_file_bytes`` and anything that
        isn't valid UTF-8. Synchronous file IO; call off the event loop."""
        root = self.workspace
        if not root.exists():
            return {}
        files: dict[str, bytes] = {}
        for rel, full in walk_files(root, self._walk_excludes):
            if len(files) >= max_files:
                return files
            try:
                if full.stat().st_size > max_file_bytes:
                    continue
                data = full.read_bytes()
            except OSError:
                continue
            if b"\x00" in data:
                continue  # a NUL is valid UTF-8, so the decode below wouldn't catch it
            try:
                _ = data.decode("utf-8")
            except UnicodeDecodeError:
                continue
            files[rel] = data
        return files

    def ensure_workspace(self) -> Path:
        """The workspace directory, materialized and ready to read or write, without
        touching the VM. This is the seam the file tools bind to.

        Counts as activity, so a session worked on purely through file tools is not reaped."""
        self._ensure_workspace()
        self.touch()
        return self.workspace

    def _ensure_workspace(self) -> None:
        # Under `_disk` so two adoptions never write one directory.
        with self._disk:
            marker = partial_marker(self.workspace)
            if marker.exists():
                # An unfinished legacy restore: discard the fragment and adopt again
                # rather than hand the agent half its files.
                shutil.rmtree(self.workspace, ignore_errors=True)
                marker.unlink(missing_ok=True)
            if not self.workspace.exists():
                # Nothing writes archives any more; one here predates that, and is read
                # back and retired (:mod:`services.sandbox.legacy_seal`).
                if not adopt_legacy_archive(self.sealed, self.workspace, self._vault):
                    self.workspace.mkdir(parents=True, exist_ok=True)
            else:
                # Both on disk means an adoption died between its last two steps; the
                # directory is the live copy.
                retire_superseded_archive(self.sealed, self.workspace)
            # Recreated every time: an adopted archive never carried them, and a missing
            # TMPDIR breaks mktemp.
            for sub in (_TMP_SUBDIR, _HOME_SUBDIR):
                (self.workspace / sub).mkdir(parents=True, exist_ok=True)

    async def _ensure_up(self) -> None:
        """Pipe the workspace into the guest, with this session's egress credentials
        granted first, so no command runs before its fence is in place."""
        if not self._vm.available and not await self._vm.wait_ready(VM_WAIT_S):
            if self._vm.failure:
                raise SandboxError(
                    f"the platform VM is unavailable: {self._vm.failure}",
                    hint=(
                        "carry on without running code where you can, and tell the "
                        + "operator the VM needs fixing for the rest"
                    ),
                )
            raise SandboxError(
                "the platform VM is still starting",
                hint="do the steps that need no code first, then try this again",
            )
        if self._credentials is None:
            self._credentials = self._vm.gateway.grant(self._egress_dir)
        try:
            await self._guest.bring_in()
        except VMError as exc:
            raise SandboxError(f"could not bring the workspace into the VM: {exc}") from exc
