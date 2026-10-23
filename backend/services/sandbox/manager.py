"""The set of live sandboxes — who owns one, how many may exist, and when each goes.

One :class:`~services.sandbox.session.SandboxSession` is a single conversation's
workspace and guest copy. The policy over the set lives here: mapping a conversation to
its session, the live-session cap, the idle reaper, the preview token index and purging a
deleted conversation.

A session's lock spans one call on one workspace; this manager's lock guards the maps that
decide which session a key names, and the tombstone protocol (``_tearing_down``) that
keeps a second session from being minted onto a workspace a teardown is still working
through.

Reaping removes guest copies only; the workspace stays on disk. What a crash leaves in the
VM is gone at the next boot.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import shutil
import time
from collections.abc import Collection, Iterable
from pathlib import Path
from typing import TYPE_CHECKING

from core.concurrency import gather_bounded
from core.periodic import PeriodicTask
from core.vault import Vault
from services.vm.host import SessionHost

from .base import safe_key
from .legacy_seal import partial_marker
from .preview import PreviewHandle
from .preview_tokens import PreviewTokens
from .session import LiveWork, SandboxSession

if TYPE_CHECKING:  # `services.egress` reads `safe_key` from this package; a real import loops.
    from services.egress import EgressPolicy

logger = logging.getLogger(__name__)

# Bounds teardowns, each of which pulls a workspace out of the VM and wipes it; shared by
# the idle sweep and the live-session cap.
_TEARDOWN_CONCURRENCY = 3

#: The longest app shutdown spends on any one session's guest copy and on the reaper's
#: in-flight teardowns.
_SESSION_STOP_TIMEOUT_S = 10.0


#: A session taken out of the live map and awaiting its teardown, with the event that
#: releases whoever is waiting on that key's teardown. See ``_detach``.
type _Detached = tuple[str, SandboxSession, asyncio.Event]


class SandboxSessionManager:
    """Maps a conversation to its live :class:`SandboxSession`, reaping idle ones —
    idle for too long, or least-recently-used once there are too many.

    Built only when the platform VM is enabled; while the VM is starting or down, a
    session's first command waits for it or reports that it is unavailable."""

    def __init__(
        self,
        vm: SessionHost,
        vault: Vault,
        *,
        egress: EgressPolicy,
        data_dir: Path,
        idle_ttl_s: float,
        reap_interval_s: float,
        excludes: Iterable[str],
        preview_startup_timeout_s: float = 20.0,
        max_sessions: int = 8,
    ) -> None:
        self._vm: SessionHost = vm
        self._vault: Vault = vault
        # Held here, not per session: the file the gateway reads must be written per key
        # before that key's first command runs (see `acquire`).
        self._egress: EgressPolicy = egress
        self._work_root: Path = data_dir / "sandbox" / "work"
        self._sealed_root: Path = data_dir / "sandbox" / "sealed"
        self._idle_ttl: float = idle_ttl_s
        self._excludes: tuple[str, ...] = tuple(excludes)
        self._preview_startup_timeout_s: float = preview_startup_timeout_s
        # Bounds the set in count; the idle TTL alone bounds only time, so a dozen threads
        # worked in rotation would all stay live. See `_over_cap`.
        self._max_sessions: int = max(1, max_sessions)
        self._sessions: dict[str, SandboxSession] = {}
        # Mutated under this manager's lock wherever it must be atomic against the reaper.
        self._preview_tokens: PreviewTokens = PreviewTokens()
        # safe key → set once its session's teardown (a reap or a purge) finishes; a
        # concurrent acquire()/purge() for that key waits on it, other keys don't.
        self._tearing_down: dict[str, asyncio.Event] = {}
        self._lock: asyncio.Lock = asyncio.Lock()
        self._reaper: PeriodicTask = PeriodicTask(
            "sandbox-reaper",
            interval_s=reap_interval_s,
            work=self.sweep,
            logger=logger,
        )
        # Held so in-flight teardowns aren't garbage-collected and `stop` can drain them.
        self._teardowns: set[asyncio.Task[None]] = set()

    @property
    def vm_starting(self) -> bool:
        """Whether the platform VM is still coming up, as opposed to an ordinary cold start."""
        return not self._vm.available and self._vm.failure is None

    @property
    def sessions(self) -> dict[str, SandboxSession]:
        """The live sessions by safe key; mutated only under this manager's lock."""
        return self._sessions

    @property
    def tearing_down(self) -> dict[str, asyncio.Event]:
        """The tombstones of keys whose session teardown is in flight."""
        return self._tearing_down

    @property
    def teardowns(self) -> set[asyncio.Task[None]]:
        """The teardown tasks still running."""
        return self._teardowns

    @property
    def preview_tokens(self) -> PreviewTokens:
        return self._preview_tokens

    @property
    def work_root(self) -> Path:
        """The directory holding every conversation's workspace."""
        return self._work_root

    @property
    def sealed_root(self) -> Path:
        """The directory that held sealed workspace archives."""
        return self._sealed_root

    @property
    def egress(self) -> EgressPolicy:
        """The allowlist policy workspaces are fenced by."""
        return self._egress

    @property
    def reaper(self) -> PeriodicTask:
        """The periodic task that sweeps idle sessions."""
        return self._reaper

    def existing(self, key: str) -> SandboxSession | None:
        """The live session for a conversation if one exists, without creating it."""
        return self._sessions.get(safe_key(key))

    @property
    def walk_excludes(self) -> tuple[str, ...]:
        """What a walk of any workspace here skips — reporting only.

        The per-turn block has no session to ask, and :mod:`services.sandbox.walk` must give
        one answer to all its readers.
        """
        return self._excludes

    def settled_workspace(self, key: str) -> Path | None:
        """This key's workspace directory if it is already on disk, else ``None``.

        Creates neither a session nor a directory: the per-turn block that tells the model
        what it already built calls this on every turn, including for conversations reaped
        long ago and for turns that never touch a file.
        """
        workspace = self._work_root / safe_key(key)
        return workspace if workspace.is_dir() else None

    async def acquire(self, key: str, *, holder: LiveWork | None = None) -> SandboxSession:
        """The session for a conversation, created (object only) on first use. If this
        key is mid-teardown, waits for that teardown rather than racing a second session
        onto the same workspace; other keys proceed immediately.

        ``holder`` is the run asking; claiming it makes the cap safe, since the exec lock
        is held for one call out of the dozens a turn makes (see
        :attr:`SandboxSession.is_claimed`). Optional for callers with no lifetime to tie a
        guest copy to, such as a preview proxy request.

        A new arrival applies the cap (:meth:`_over_cap`): it reaps the least-recently-used
        idle sessions down to the ceiling before returning. Their files stay on disk."""
        safe = safe_key(key)
        evicted: list[_Detached] = []
        while True:
            other: asyncio.Event | None = None
            async with self._lock:
                session = self._sessions.get(safe)
                if session is None:
                    other = self._tearing_down.get(safe)
                    if other is None:
                        session = self._new_session(safe, self._egress.allow_dir(key))
                        self._sessions[safe] = session
                        # Only a new arrival applies the cap, not every tool call.
                        evicted = self._detach(self._over_cap(keep={safe}))
                if session is not None:
                    session.touch()
                    session.hold(holder)
                    break
            assert other is not None
            _ = await other.wait()
        if not session.is_warm:
            # On disk before the first command reaches the gateway, or it would refuse
            # everything already allowed. Later grants rewrite the file.
            _ = await self._egress.materialise(key)
        # Awaited so the host is under the cap before the caller brings a workspace in.
        await self._tear_down(evicted)
        return session

    def _over_cap(self, *, keep: Collection[str]) -> list[str]:
        """The session keys to reap so the live set fits under the cap, longest-idle
        first. Called under the manager lock.

        ``keep`` (the arriving session) always survives. Otherwise only a session nobody is
        using is a candidate (:attr:`SandboxSession.is_displaceable`). The cap is a ceiling,
        not a deadline: if everything live is in use the set runs over and the idle sweep
        collects the overflow afterwards.

        The vault is not consulted: displacing wipes only the guest copy, and a locked vault
        must not stop the cap being enforced."""
        overflow = len(self._sessions) - self._max_sessions
        if overflow <= 0:
            return []
        now = time.monotonic()
        idle = sorted(
            (
                (session.idle_seconds(now), key)
                for key, session in self._sessions.items()
                if key not in keep and session.is_displaceable
            ),
            reverse=True,
        )
        return [key for _, key in idle[:overflow]]

    def _detach(self, keys: Iterable[str]) -> list[_Detached]:
        """Take sessions out of the live map, leaving a per-key teardown tombstone
        behind. Called under the manager lock, by the idle sweep and the cap.

        Detaching is separate from tearing down: the map mutation must be atomic against a
        concurrent ``acquire()``/``purge()``, while the slow teardown must not hold the
        lock. The events are what the same key's acquire/purge waits on."""
        detached: list[_Detached] = []
        for key in keys:
            session = self._sessions.pop(key, None)
            if session is None:
                continue
            self._mark_preview_stopped(session)
            self._preview_tokens.drop(key)
            event = asyncio.Event()
            self._tearing_down[key] = event
            detached.append((key, session, event))
        return detached

    async def _reattach(self, detached: list[_Detached]) -> None:
        """Undo a :meth:`_detach` whose teardown failed. Dropped instead, the session would be
        a guest copy in nobody's map behind a tombstone that every later ``acquire()`` for
        the key waits on forever. The tombstone keeps anything else from taking the key."""
        async with self._lock:
            for key, session, _event in detached:
                _ = self._sessions.setdefault(key, session)
                _ = self._tearing_down.pop(key, None)
        for _key, _session, event in detached:
            event.set()

    async def _tear_down(self, detached: list[_Detached]) -> None:
        """Tear detached sessions down on a task the manager owns, and wait for it.

        The sessions belong to other conversations, so a cancelled caller (the operator
        pressing Stop) must not abort the teardown and strand a guest copy behind a
        tombstone. ``shield`` lets the caller stop waiting while the teardown finishes;
        :meth:`stop` drains it, since a half-brought-out guest copy is worse than a slow
        shutdown."""
        if not detached:
            return
        task = asyncio.create_task(self._tear_down_detached(detached))
        self._teardowns.add(task)
        task.add_done_callback(self._teardowns.discard)
        await asyncio.shield(task)

    async def _tear_down_detached(self, detached: list[_Detached]) -> None:
        """Tear detached sessions down off the manager lock, a few at a time, then release
        each tombstone. Each leg isolates its own failure so one cannot strand the others or
        leave a tombstone set forever."""

        async def down(key: str, session: SandboxSession, event: asyncio.Event) -> None:
            torn_down = False
            try:
                await session.shutdown()
                torn_down = True
            except Exception:  # one bad teardown must not stall the rest
                logger.warning(
                    "sandbox %s: teardown failed; leaving the session live for the sweep",
                    key,
                    exc_info=True,
                )
            finally:
                if torn_down:
                    async with self._lock:
                        _ = self._tearing_down.pop(key, None)
                    event.set()
                else:
                    # Put it back for the next idle sweep to retry.
                    await self._reattach([(key, session, event)])

        _ = await gather_bounded([down(*item) for item in detached], _TEARDOWN_CONCURRENCY)

    def _new_session(self, safe: str, egress_dir: Path) -> SandboxSession:
        return SandboxSession(
            safe,
            workspace=self._work_root / safe,
            sealed=self._sealed_root / f"{safe}.tar.enc.gz",
            egress_dir=egress_dir,
            vm=self._vm,
            vault=self._vault,
            excludes=self._excludes,
        )

    async def start_preview(self, key: str, command: list[str], port: int) -> PreviewHandle:
        """Start (or replace) the conversation's live preview and index its token."""
        session = await self.acquire(key)
        safe = safe_key(key)
        token = secrets.token_urlsafe(32)
        # Outside the manager lock so the readiness wait doesn't stall other conversations;
        # the session's own lock marks it busy so the reaper defers.
        handle = await session.start_preview(
            command, port, token=token, startup_timeout_s=self._preview_startup_timeout_s
        )
        async with self._lock:
            self._preview_tokens.drop(safe)  # one preview per conversation
            self._preview_tokens.index(token, safe)
        return handle

    def resolve_preview(self, token: str) -> PreviewHandle | None:
        """The running preview a proxy request names, or None. Touches the session so
        viewing keeps it from the idle reaper. Sync so it reads the maps atomically."""
        safe = self._preview_tokens.owner(token)
        if safe is None:
            return None
        session = self._sessions.get(safe)
        if session is None or session.preview is None or session.preview.token != token:
            return None
        session.touch()
        return session.preview

    def preview_status(self, token: str) -> str:
        """Whether a `view.live` token names a running preview ("running"), was torn down
        without an explicit stop ("stopped"), or is unrecognized ("unknown").

        Unlike `resolve_preview` it does not touch the session, so a status poll can't keep
        an idle preview warm."""
        safe = self._preview_tokens.owner(token)
        if safe is not None:
            session = self._sessions.get(safe)
            if session is not None and session.preview is not None:
                if session.preview.token == token:
                    return "running"
        return "stopped" if self._preview_tokens.was_stopped(token) else "unknown"

    def _mark_preview_stopped(self, session: SandboxSession) -> None:
        """Tombstone a session's preview token as stopped-without-a-signal (idle
        reap or purge). Call *before* the session's preview is torn down."""
        if session.preview is not None:
            self._preview_tokens.tombstone(session.preview.token)

    async def stop_preview(self, key: str) -> None:
        """Tear down the conversation's preview, leaving the exec session intact."""
        safe = safe_key(key)
        async with self._lock:
            session = self._sessions.get(safe)
            self._preview_tokens.drop(safe)
            if session is not None:
                await session.stop_preview()

    async def purge(self, key: str) -> None:
        """Delete a conversation's sandbox outright: stop any live session and remove its
        workspace and any leftover sealed archive. Idempotent, and works while the vault is
        locked.

        Registers in ``_tearing_down`` like a reap: it waits for a reap already mid-flight
        for this key, and a concurrent ``acquire()`` waits for it, so nothing recreates a
        session onto files being removed."""
        safe = safe_key(key)
        my_event = asyncio.Event()
        session: SandboxSession | None = None
        while True:
            async with self._lock:
                other = self._tearing_down.get(safe)
                if other is None:
                    session = self._sessions.pop(safe, None)
                    if session is not None:
                        self._mark_preview_stopped(session)
                        self._preview_tokens.drop(safe)
                    self._tearing_down[safe] = my_event
                    break
            _ = await other.wait()
        try:
            if session is not None:
                await session.discard()
            await asyncio.to_thread(self._purge_disk, safe)
        finally:
            async with self._lock:
                _ = self._tearing_down.pop(safe, None)
            my_event.set()

    def _purge_disk(self, safe: str) -> None:
        workspace = self._work_root / safe
        shutil.rmtree(workspace, ignore_errors=True)
        partial_marker(workspace).unlink(missing_ok=True)
        (self._sealed_root / f"{safe}.tar.enc.gz").unlink(missing_ok=True)

    async def start(self) -> None:
        """Launch the idle reaper."""
        await self._reaper.start()

    async def stop(self) -> None:
        await self._reaper.stop()
        # Drained, never cancelled (an interrupted teardown leaves a half-brought-out guest
        # copy), and before taking the lock, which each needs to release its tombstone.
        if self._teardowns:
            _ = await asyncio.wait(self._teardowns, timeout=_SESSION_STOP_TIMEOUT_S)
        async with self._lock:
            # Parallel and each bounded, so shutdown isn't N serial timeouts. A session
            # cut off by its cap is left behind; its guest copy is gone at the next boot.
            async def _stop(session: SandboxSession) -> None:
                try:
                    await asyncio.wait_for(session.shutdown(), _SESSION_STOP_TIMEOUT_S)
                except Exception:  # tear the rest down regardless
                    logger.warning("sandbox: session teardown did not finish", exc_info=True)

            _ = await asyncio.gather(*(_stop(s) for s in list(self._sessions.values())))
            self._sessions.clear()
            self._preview_tokens.clear()

    async def sweep(self) -> None:
        now = time.monotonic()
        async with self._lock:
            # Reaping is lossless (the guest copy goes, the files stay), so who holds the
            # session is ignored, down to a run parked on an unanswered approval.
            stale = [
                key
                for key, s in self._sessions.items()
                if not s.is_busy and s.idle_seconds(now) >= self._idle_ttl
            ]
            # Detach under the lock so a concurrent acquire() can't mint a second session
            # onto the workspace; the slow teardown runs outside it.
            detached = self._detach(stale)

        await self._tear_down(detached)
