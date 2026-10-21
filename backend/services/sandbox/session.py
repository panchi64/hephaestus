"""One conversation's live sandbox: its workspace and the guest copy commands run in.

A conversation gets one workspace, a host-side directory that holds the agent's files
for as long as the conversation exists. Commands do not run here; they run in the
platform VM (:mod:`services.vm`), against a guest copy of the workspace that is piped in
before a command and back out after it. Nothing is created until the agent first runs
code, and an idle session is reaped by wiping the guest copy, which costs nothing: a
`.venv` the agent installed, a `dist/` it built and the `.git` of a repository it cloned
are all in the host directory, and come back with the next command.

Each command is confined to its own workspace inside the VM, and reaches the network only
through the egress gateway with this session's credentials, which hold it to the
conversation's allowlist. Installing a package and burning CPU are what the workspace is
*for* and cost no approval; what is fenced is where bytes may go.

A session knows only about itself. *When* it is created and *when* it is reaped are
policies over the whole set, and live in :mod:`services.sandbox.manager`.
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
    reports as a proxy refusal rather than as a DNS failure that reads like no network
    exists."""
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

    A protocol rather than the class because the sandbox has no business depending on the
    run substrate to answer a question this small — and, more importantly, because a claim
    read this way is **released by asking, never by a hand-back**. The caller that would
    owe the hand-back is precisely the one that gets cancelled, times out, or dies with an
    unhandled error, and a claim leaked on those paths pins a guest copy for the life of
    the process.
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
        #: Where this key's pre-existing sealed archive would be, if the installation
        #: ever ran the build that wrote them. Read once and retired — see
        #: :mod:`services.sandbox.legacy_seal`.
        self.sealed: Path = sealed
        # This workspace's allowlist directory, owned by the egress policy, which
        # rewrites it whenever the operator approves a domain; the gateway re-reads it,
        # so a grant lands on the next request.
        self._egress_dir: Path = egress_dir
        self._vm: SessionHost = vm
        self._guest: GuestWorkspace = GuestWorkspace(vm, key, workspace)
        self._credentials: str | None = None
        self._preview_name: str = f"preview-{key}"
        self._vault: Vault = vault
        # What a walk of this workspace skips — reporting only, never deletion. See
        # :mod:`services.sandbox.walk`.
        self._walk_excludes: tuple[str, ...] = tuple(excludes)
        self._preview: PreviewHandle | None = None
        self._last_used: float = time.monotonic()
        self._lock: asyncio.Lock = asyncio.Lock()
        # Orders the multi-step disk transitions on this workspace: materialising it
        # (`_ensure_workspace`, which may still be adopting a sealing-era archive) against
        # the file tools, which deliberately take no session lock. A thread lock rather
        # than the asyncio one because every side is synchronous and some of them do not
        # run on the loop at all.
        self._disk: threading.RLock = threading.RLock()
        self._holders: list[LiveWork] = []

    @property
    def is_busy(self) -> bool:
        """A call is in flight on this session *right now* — one exec, one preview
        launch, one teardown. Deliberately narrow: it is the lock, and the lock spans a
        single call, not the turn the call belongs to. See :attr:`is_claimed`."""
        return self._lock.locked()

    def hold(self, holder: LiveWork | None) -> None:
        """Claim this session for a unit of work, until that work reaches a terminal
        state. Idempotent — re-claiming from the same run is what every later tool call
        in a turn does. ``None`` claims nothing: it is a caller with no lifetime to tie a
        guest copy to (a proxy request, a route, a test), and it still costs the prune."""
        self._holders = [h for h in self._holders if not h.is_terminal and h is not holder]
        if holder is not None:
            self._holders.append(holder)

    @property
    def is_claimed(self) -> bool:
        """Whether a run that has not finished is still working in here.

        The gap this covers is the one the lock cannot: a turn is a *sequence* of execs
        with model thinking in between, and between two of them the session looks idle
        while being very much in use. Pruned on read rather than on release, because the
        run that would do the releasing is the one being cancelled."""
        self._holders = [h for h in self._holders if not h.is_terminal]
        return bool(self._holders)

    @property
    def is_displaceable(self) -> bool:
        """Whether displacing this session at the live-session cap would take it from
        nobody. Three ways it would not:

        - a call is in flight, and wiping the guest copy mid-exec fails the tool call the
          operator is watching;
        - a run that has not finished is between tool calls, and while its files would
          survive the reap, the live process and system state around them would not —
          an interpreter left running, a server it started, a shell's directory;
        - a preview is live, and reaping it drops the token the proxy resolves, turning a
          page the operator may be looking at into a 404.

        The idle sweep asks a deliberately different question (busy, plus the TTL): a
        preview nobody has loaded in half an hour, or a run parked on an approval nobody
        answered, *should* be collected eventually. Time bounds everything; the count
        bounds only what nobody is using right now.
        """
        return not self.is_busy and not self.is_claimed and self._preview is None

    @property
    def is_warm(self) -> bool:
        """True when the guest already holds this workspace, so a run executes at once.
        False before the first run (or after a reap), when ``run()`` must first pipe the
        workspace in — a wait the caller may want to announce."""
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
        """The environment a home command runs with: the package-manager defaults, and the
        egress proxy, with credentials only when it may reach the network. ``HOME`` is left
        to the confinement, which points it at the home so ``~/projects`` means what the
        agent was told."""
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
        """Stage a file *into* this session's workspace, whether or not the guest
        holds it. Writes the host copy, which the next code run pushes in, and it
        outlives a reap like everything else in the workspace. Guards against escape —
        the same invariant as :meth:`read_file`, in reverse."""
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
            # Wait until it answers over HTTP, so the iframe's first fetch (which fires
            # the instant `view.live` is emitted) lands on a real response. A server that
            # is merely slow is still worth opening; one that has exited is not.
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
        """Bring the work out of the guest and wipe the guest copy. The workspace stays
        exactly where it is.

        This is the whole of a reap. A conversation's files are not what a reap is trying
        to reclaim — the guest copy, its processes and its preview are — so they are left
        on disk, which is also the only way a `.venv` the agent installed or the `.git` of
        a repository it cloned survive to the next turn."""
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
            # The guest copy goes with the VM if it cannot be reached; the host copy is
            # the workspace and is untouched either way.
            logger.warning("sandbox %s: could not bring the guest copy out", self.key)
            self._guest.forget()
        if self._credentials is not None:
            self._vm.gateway.revoke(self._credentials)
            self._credentials = None

    def collect_text_files(
        self, *, max_file_bytes: int = 262_144, max_files: int = 2000
    ) -> dict[str, bytes]:
        """The workspace's text files (relpath → bytes) for a history snapshot — the
        same files every other walk reports, minus binaries and oversized ones. Prunes
        the bloat (caches, virtualenvs, ``node_modules``, ``.git``), skips files over
        ``max_file_bytes`` and anything that isn't valid UTF-8. Empty when the
        conversation has never put anything in its workspace. Synchronous file IO —
        call off the event loop."""
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
                continue  # NUL byte ⇒ binary (a NUL is valid UTF-8, so the
                # decode check below wouldn't catch it) — history is text only
            try:
                _ = data.decode("utf-8")
            except UnicodeDecodeError:
                continue  # binary — skipped (history is code + text diffs)
            files[rel] = data
        return files

    def ensure_workspace(self) -> Path:
        """The workspace directory, materialized and ready to read or write, **without
        touching the VM**. This is the seam the file tools bind to: browsing,
        reading and editing files costs no VM round trip at all.

        Counts as activity (``touch``), so a session being worked on purely through file
        tools is not reaped out from under the run that is using it."""
        self._ensure_workspace()
        self.touch()
        return self.workspace

    def _ensure_workspace(self) -> None:
        # Under `_disk` so the one-way adoption below cannot interleave with another
        # materialisation — two writers on one directory. See `_disk`.
        with self._disk:
            marker = partial_marker(self.workspace)
            if marker.exists():
                # A sealing-era restore that never finished: the directory holds less
                # than the archive beside it, so throw the fragment away and let the
                # adoption run again rather than hand the agent half its files.
                shutil.rmtree(self.workspace, ignore_errors=True)
                marker.unlink(missing_ok=True)
            if not self.workspace.exists():
                # Nothing writes an archive any more; one still sitting here was
                # written by the build that sealed on reap, and this is where it is
                # read back and retired. See :mod:`services.sandbox.legacy_seal`.
                if not adopt_legacy_archive(self.sealed, self.workspace, self._vault):
                    self.workspace.mkdir(parents=True, exist_ok=True)
            else:
                # Both on disk means an adoption died between its last two steps. The
                # directory is the live copy, so the archive is residue.
                retire_superseded_archive(self.sealed, self.workspace)
            # The build-temp dir is not something a walk reports, and an adopted archive
            # never carried it, so recreate it every time — a missing TMPDIR breaks
            # mktemp and silently shrinks pip's scratch space.
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
