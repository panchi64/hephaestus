"""The set of live sandboxes — who owns one, how many may exist, and when each goes.

One :class:`~services.sandbox.session.SandboxSession` is a single conversation's
workspace and guest copy, and knows nothing about the others. Everything that is a
*policy over the set* lives here instead: mapping a conversation to its session,
the live-session cap, the idle reaper, the preview token index, purging a deleted
conversation, collecting the forks an unclean shutdown stranded, and forking a
session for a delegated agent (the copy and the merge themselves are
:mod:`services.sandbox.fork`'s; which key is the parent is a fact about the set).

The split follows the lock. A session's lock spans one call on one workspace; this
manager's lock guards the maps that decide which session a key even names, and the
tombstone protocol (``_tearing_down``) that keeps a second session from being minted
onto a workspace a teardown is still working through. Two locks with two jobs, and
neither file has a reason to change when the other's job does.

Reaping is about guest copies and nothing else. An ordinary conversation's workspace is
left on disk untouched, so a stranded *directory* is not a problem to solve — it is
simply that conversation's files, waiting. The one thing a crash can strand that
nobody will ever ask for again is a delegated agent's fork, and the idle sweep
collects those (:meth:`_collect_orphan_forks`). What a crash leaves in the VM is gone
at the next boot, which starts from the clean image.
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
from core.fork import MergeReport
from core.periodic import PeriodicTask
from core.vault import Vault
from services.vm import PlatformVM

from .base import SandboxError, safe_key
from .fork import fork_marker, orphan_fork_keys
from .legacy_seal import partial_marker
from .preview import PreviewHandle
from .preview_tokens import PreviewTokens
from .session import LiveWork, SandboxSession

if TYPE_CHECKING:  # `services.egress` reads `safe_key` from this package — importing it
    # for real here would close the loop, and the type is all this module needs.
    from services.egress import EgressPolicy

logger = logging.getLogger(__name__)

# How many reaped sessions are torn down concurrently — each one pulls a workspace out
# of the VM and wipes it, so a mass reap is bounded rather than left to thrash the
# host, but no longer serial, so unrelated conversations aren't
# stalled behind one another. Shared by both reasons a session is reaped: the idle
# sweep, and a new session displacing an old one at the live-session cap.
_TEARDOWN_CONCURRENCY = 3

#: The longest app shutdown spends on any one session's guest copy (and on waiting for the
#: reaper's in-flight teardowns). A teardown that has not returned by now is not going
#: to, and the process exiting is worth more than the one stop.
_SESSION_STOP_TIMEOUT_S = 10.0

# How many stranded forks one sweep collects. Every key in a batch is tombstoned for
# the whole batch, and an `acquire()` that lands on a tombstone waits with nothing to
# show the operator — so the batch is kept to a couple of rounds rather than however
# many a crashed process happened to leave. The next sweep takes the next slice, which
# is soon enough for directories nobody is waiting on.
_ORPHAN_FORKS_PER_SWEEP = 2 * _TEARDOWN_CONCURRENCY


#: A session taken out of the live map and awaiting its teardown, with the event that
#: releases whoever is waiting on *that* key's teardown. See ``_detach``.
type _Detached = tuple[str, SandboxSession, asyncio.Event]


class SandboxSessionManager:
    """Maps a conversation to its live :class:`SandboxSession`, reaping idle ones —
    idle for too long, or least-recently-used once there are too many.

    Built only when the platform VM is enabled; while the VM is starting or down, a
    session's first command waits for it or reports that it is unavailable."""

    def __init__(
        self,
        vm: PlatformVM,
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
        self._vm = vm
        self._vault = vault
        # What each workspace may reach. The manager holds it (rather than each session)
        # because writing the file the gateway reads is a *per-key* act that has to happen
        # before that key's first command runs — see `acquire`.
        self._egress = egress
        self._work_root = data_dir / "sandbox" / "work"
        self._sealed_root = data_dir / "sandbox" / "sealed"
        self._idle_ttl = idle_ttl_s
        self._excludes = tuple(excludes)
        self._preview_startup_timeout_s = preview_startup_timeout_s
        # How many conversations may hold a guest copy at once. The idle TTL bounds a
        # session in *time*; this bounds the set in *count*, which the TTL alone never does
        # — a dozen threads worked on in rotation all stay inside the window. See
        # `_over_cap`.
        self._max_sessions = max(1, max_sessions)
        self._sessions: dict[str, SandboxSession] = {}
        # Which token names which preview, and which ones we killed — see
        # :mod:`services.sandbox.preview_tokens`. Manipulated under this manager's lock
        # wherever it has to be atomic against the reaper.
        self._preview_tokens = PreviewTokens()
        # safe key → set once its (former) session's teardown (a sweep's reap, or
        # a purge) is in flight. A concurrent acquire()/purge() for THIS key waits
        # on it; every other key is unaffected (sandbox-02).
        self._tearing_down: dict[str, asyncio.Event] = {}
        self._lock = asyncio.Lock()
        self._reaper = PeriodicTask(
            "sandbox-reaper",
            interval_s=reap_interval_s,
            work=self._sweep,
            logger=logger,
        )
        # Teardowns in flight, owned by the manager rather than by whoever triggered
        # them — see `_tear_down`. Held so they are not garbage-collected halfway
        # through and so `stop` can drain them.
        self._teardowns: set[asyncio.Task] = set()

    @property
    def vm_starting(self) -> bool:
        """Whether the platform VM is still coming up — lets a caller (e.g. the
        ``code_execute`` tool) tell an ordinary cold start from one waiting on the VM."""
        return not self._vm.available and self._vm.failure is None

    def existing(self, key: str) -> SandboxSession | None:
        """The live session for a conversation if one exists, **without creating** it,
        so a turn that never touched the sandbox triggers no workspace/history work."""
        return self._sessions.get(safe_key(key))

    @property
    def walk_excludes(self) -> tuple[str, ...]:
        """What a walk of any workspace here skips — reporting and merge safety only.

        Exposed because :mod:`services.sandbox.walk` is one answer four readers have to
        agree on, and the per-turn block is the one reader that has no session to ask.
        Re-reading the setting instead would make it a second source that only happens
        to match.
        """
        return self._excludes

    def settled_workspace(self, key: str) -> Path | None:
        """This key's workspace directory if it is already on disk, else ``None`` —
        **creating nothing**, neither a session nor a directory.

        A live session is not the question. A conversation reaped an hour ago has no
        session and all of its files, which is exactly the case the caller here cares
        about: the per-turn block that tells the model what it already built. Going
        through `acquire`/`ensure_workspace` to answer it would mint a session and an
        empty directory for every turn that never touches a file — the cost the whole
        lazy design exists to avoid.
        """
        workspace = self._work_root / safe_key(key)
        return workspace if workspace.is_dir() else None

    async def acquire(self, key: str, *, holder: LiveWork | None = None) -> SandboxSession:
        """The session for a conversation, created (object only) on first use. If this
        key is mid-teardown from a concurrent sweep/purge, waits for THAT teardown
        specifically rather than racing a second session onto the same workspace
        path; every other key proceeds immediately (sandbox-02).

        ``holder`` is the run asking, and claiming it here is what makes the cap safe:
        without it the only "in use" signal is the exec lock, which is held for one call
        out of the dozens a turn makes. See :attr:`SandboxSession.is_claimed`. Optional
        because not every caller is a turn — a proxy request resolving a preview, a route,
        a test — and those genuinely have no lifetime to tie a guest copy to.

        Admitting a new session is also where the **cap** is applied (:meth:`_over_cap`):
        a new arrival reaps the least-recently-used idle sessions back down to the ceiling
        before it returns, which makes the ceiling a policy instead of an accident. Nothing
        is lost by the reap: a reaped session's files stay on disk and are there the next
        time that conversation runs code, exactly as after an idle reap."""
        safe = safe_key(key)
        evicted: list[_Detached] = []
        while True:
            async with self._lock:
                session = self._sessions.get(safe)
                if session is None:
                    other = self._tearing_down.get(safe)
                    if other is None:
                        session = self._new_session(safe, self._egress.allow_dir(key))
                        self._sessions[safe] = session
                        # Only a *new* arrival applies the cap: finding a session already
                        # live is the steady state, and re-reaping on every tool call
                        # would make the cap a per-call sweep.
                        evicted = self._detach(self._over_cap(keep={safe}))
                if session is not None:
                    session.touch()
                    session.hold(holder)
                    break
            await other.wait()
        if not session.is_warm:
            # The allowlist has to be on disk before the first command can reach the
            # gateway, or it would refuse everything the operator has already allowed. A
            # warm session already has it, and every later grant rewrites the file.
            await self._egress.materialise(key)
        # Awaited rather than backgrounded, because the caller is about to bring a
        # workspace in: returning before the ones it displaced are actually gone would leave
        # the host over the cap at exactly the moment the cap matters. On a task of the
        # manager's own, because those sessions are *other* conversations' — `_tear_down`.
        await self._tear_down(evicted)
        return session

    def _over_cap(self, *, keep: Collection[str]) -> list[str]:
        """The session keys to reap so the live set fits under the cap, longest-idle
        first. Called under the manager lock.

        ``keep`` is what the admission is *about* and must survive it: the arriving
        session, and — for a fork — the parent it was copied from, mid-work by the very
        fact that it has just delegated. Beyond those, only a session nobody is using is a
        candidate — see :attr:`SandboxSession.is_displaceable` for the three ways that is
        decided. The cap is a resource ceiling, not a deadline: if everything live is in use
        the set simply runs over and the idle sweep collects the overflow once the work
        finishes — the same choice ``ConversationStore._trim_cache`` makes for pinned trees.

        The vault is not consulted. Displacing a session wipes its guest copy and
        leaves its files where they are, so there is nothing here that needs a key — and
        a locked vault used to mean the cap stopped being enforced at all, which is the
        one state in which guest copies accumulate unchecked."""
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
        behind. Called under the manager lock, by both reasons a session is reaped: the
        idle sweep and the live-session cap.

        Detaching and tearing down are separate halves on purpose. The map mutation has
        to be atomic against a concurrent ``acquire()``/``purge()`` — otherwise a second
        session is minted onto a workspace mid-teardown — while the teardown itself is
        slow and must not hold the lock for the sum of every one (sandbox-02). The events
        are what the *same* key's acquire/purge waits on; every other key proceeds."""
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
        """Undo a :meth:`_detach` whose teardown must not happen after all — a teardown
        that failed, a merge that reported nothing. Dropped there instead, a session is
        lost where neither sweep nor purge can find it: a guest copy in nobody's map over
        a live workspace, behind a tombstone every later ``acquire()`` for that key waits
        on forever. Nothing can have taken the key meanwhile — holding acquire and purge
        off is what that tombstone is for."""
        async with self._lock:
            for key, session, _event in detached:
                self._sessions.setdefault(key, session)
                self._tearing_down.pop(key, None)
        for _key, _session, event in detached:
            event.set()

    async def _tear_down(self, detached: list[_Detached]) -> None:
        """Tear detached sessions down on a task the *manager* owns, and wait for it.

        The waiting is the caller's; the teardown is not. Both reasons a session is
        detached tear down somebody else's conversation — the cap displaces whoever was
        least-recently-used, the sweep collects whoever went idle — and neither should
        die because the task that happened to trigger it was cancelled. A teardown is
        several VM round trips, so the window is wide: the operator presses Stop,
        the acquiring task unwinds, and an unrelated conversation is left with a live
        guest copy in nobody's map, behind a tombstone every later acquire waits on.

        ``shield`` is what separates the two: a cancelled caller stops waiting, and the
        teardown it started finishes regardless. :meth:`stop` drains what is still in
        flight rather than cancelling it — a guest copy half brought out is the one
        outcome worse than a slow shutdown."""
        if not detached:
            return
        task = asyncio.create_task(self._tear_down_detached(detached))
        self._teardowns.add(task)
        task.add_done_callback(self._teardowns.discard)
        await asyncio.shield(task)

    async def _tear_down_detached(self, detached: list[_Detached]) -> None:
        """Tear detached sessions down off the manager lock, a few at a time, then
        release each one's tombstone. One failed teardown must not strand the others
        — or, worse, leave a tombstone set forever and hang every later acquire for that
        conversation — so each leg isolates its own failure and releases in a ``finally``."""

        async def down(key: str, session: SandboxSession, event: asyncio.Event) -> None:
            torn_down = False
            try:
                await session.shutdown()
                torn_down = True
            except Exception:  # noqa: BLE001 — one bad teardown must not stall the rest
                logger.warning(
                    "sandbox %s: teardown failed; leaving the session live for the sweep",
                    key,
                    exc_info=True,
                )
            finally:
                if torn_down:
                    async with self._lock:
                        self._tearing_down.pop(key, None)
                    event.set()
                else:
                    # A teardown that did not happen must not lose the session with it —
                    # put it back and let the next idle sweep try again.
                    await self._reattach([(key, session, event)])

        await gather_bounded([down(*item) for item in detached], _TEARDOWN_CONCURRENCY)

    def _new_session(
        self, safe: str, egress_dir: Path, *, ephemeral: bool = False
    ) -> SandboxSession:
        return SandboxSession(
            safe,
            workspace=self._work_root / safe,
            sealed=self._sealed_root / f"{safe}.tar.enc.gz",
            egress_dir=egress_dir,
            vm=self._vm,
            vault=self._vault,
            excludes=self._excludes,
            ephemeral=ephemeral,
        )

    async def fork(
        self, parent_key: str, child_key: str, *, holder: LiveWork | None = None
    ) -> SandboxSession:
        """A delegated agent's own session, over a copy of ``parent_key``'s workspace.

        The child is a full session — its own guest copy and egress credentials — over a
        copy of the parent's files and behind the *parent's* allowlist: a delegated agent
        reaches what the conversation that delegated to it reaches, and a grant approved for
        one is not a second thing to approve for the other. ``holder`` is the child's run,
        and passing it keeps the fork out of the cap's reach — displacing one *deletes*
        it, where displacing an ordinary session only wipes its guest copy — and
        claims the *parent*, mid-work by the very fact that it has just delegated."""
        parent = await self.acquire(parent_key, holder=holder)
        safe = safe_key(child_key)
        async with self._lock:
            if safe in self._sessions or safe in self._tearing_down:
                raise SandboxError(f"a workspace already exists for {child_key!r}")
            child = self._new_session(safe, self._egress.allow_dir(parent_key), ephemeral=True)
            self._sessions[safe] = child
            child.hold(holder)
            evicted = self._detach(self._over_cap(keep={safe, parent.key}))
        # Both halves have to agree on the key — a grant written under the child's own
        # would land in a file the child's credentials never read. After the map, so a
        # refusal above leaves no alias.
        self._egress.share(child_key, parent_key)
        # Before the copy, not after: what this displaced is already out of the live map
        # behind a tombstone, and a failed copy would strand its acquires on an unset event.
        await self._tear_down(evicted)
        try:
            # The parent's own call: the copy must hold the workspace's disk lock —
            # `clone_into`.
            child.fork_manifest = await asyncio.to_thread(parent.clone_into, child.workspace)
        except BaseException:
            # A fork with no copy is a session nobody can use, and reap-bait left in the map
            # over a copy nothing will ever merge — and a Stop into the copy, the longest
            # await here, is not an `Exception`. `purge` takes the alias down with it.
            await asyncio.shield(self.purge(child_key))
            raise
        return child

    async def merge_back(self, child_key: str, parent_key: str) -> MergeReport:
        """Land what a forked workspace changed back in its parent, then delete the fork.

        Deleted whichever way the merge *went*: a report of conflicts has already told the
        caller which paths did not come across, and keeping a second copy of the parent's
        files in the hope someone revisits them is how disks fill up. A merge that produced
        no report at all — a vault re-locked mid-delegation, a Stop mid-walk — is the other
        case: the fork is put back, being the only copy of that agent's work with nothing
        reported for anyone to act on. Only a *fork* may be the child, precisely because
        deleting it is the last thing this does: an ordinary conversation's key, from a
        caller with its arguments the wrong way round, would land everything that
        conversation holds in another's workspace and then go with its archive; one key
        twice would wait out the teardown it is itself holding."""
        safe = safe_key(child_key)
        async with self._lock:
            child = self._sessions.get(safe)
            if child is None or not child.ephemeral or safe == safe_key(parent_key):
                raise SandboxError(f"there is no forked workspace for {child_key!r}")
            # Out of the live map for the whole merge: a fork displaced meanwhile is
            # *deleted*, leaving this walk an empty directory to report as a clean landing.
            detached = self._detach([safe])
        try:
            # The fork's work comes out of the VM before the walk, so the walk judges
            # everything the child wrote and nothing writes after it.
            await child.discard()
            parent = await self.acquire(parent_key)
            work = child.ensure_workspace()
            report = await asyncio.to_thread(parent.merge_fork, work, child.fork_manifest)
        except BaseException:
            # Shielded, like the teardown it replaces: a cancellation delivered here too
            # would leave the fork in no map, behind a tombstone nothing ever releases.
            await asyncio.shield(self._reattach(detached))
            raise
        await self._tear_down(detached)
        return report

    async def start_preview(self, key: str, command: list[str], port: int) -> PreviewHandle:
        """Start (or replace) the conversation's live preview and index its token."""
        session = await self.acquire(key)
        safe = safe_key(key)
        token = secrets.token_urlsafe(32)
        # Launch outside the manager lock — the readiness wait must not stall other
        # conversations; the session's own lock marks it busy so the reaper defers.
        handle = await session.start_preview(
            command, port, token=token, startup_timeout_s=self._preview_startup_timeout_s
        )
        async with self._lock:
            self._preview_tokens.drop(safe)  # one preview per conversation
            self._preview_tokens.index(token, safe)
        return handle

    def resolve_preview(self, token: str) -> PreviewHandle | None:
        """The running preview a proxy request names, or None. Touches the session
        so active viewing keeps it warm (the idle reaper won't evict it). Sync (no
        await) so it reads the maps atomically against the reaper."""
        safe = self._preview_tokens.owner(token)
        if safe is None:
            return None
        session = self._sessions.get(safe)
        if session is None or session.preview is None or session.preview.token != token:
            return None
        session.touch()
        return session.preview

    def preview_status(self, token: str) -> str:
        """Whether a `view.live` token still names a running preview, was torn down
        without an explicit stop (idle-reaped or the conversation was purged), or is
        unrecognized. Read-only — unlike `resolve_preview`, a status check must not
        itself keep an otherwise-idle preview warm. Lets the frontend tell "the
        sandbox went idle and killed it" apart from a merely-still-loading iframe."""
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
        """Delete a conversation's sandbox outright — stop any live session and remove
        its workspace from disk, along with any sealing-era archive still sitting beside
        it. Called when the conversation is deleted, so nothing is kept. Idempotent and
        safe for a conversation with no live session, and works while the vault is
        locked (it only destroys).

        Registers itself in ``_tearing_down`` (the same gate a sweep's reap uses)
        for the duration of its own teardown+delete: if a sweep is already mid-reap
        for this key we wait for that first, and a concurrent ``acquire()`` for
        this key waits for us in turn — so nothing ever recreates a session onto
        files we're in the middle of removing (sandbox-02)."""
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
            await other.wait()
        try:
            if session is not None:
                await session.discard()
            await asyncio.to_thread(self._purge_disk, safe)
        finally:
            # A fork's allowlist alias ends with the workspace it named — nothing ever
            # looks that key up again. An ordinary conversation never has one.
            self._egress.unshare(key)
            async with self._lock:
                self._tearing_down.pop(safe, None)
            my_event.set()

    def _purge_disk(self, safe: str) -> None:
        workspace = self._work_root / safe
        shutil.rmtree(workspace, ignore_errors=True)
        partial_marker(workspace).unlink(missing_ok=True)
        fork_marker(workspace).unlink(missing_ok=True)
        (self._sealed_root / f"{safe}.tar.enc.gz").unlink(missing_ok=True)

    async def start(self) -> None:
        """Launch the idle reaper."""
        await self._reaper.start()

    async def stop(self) -> None:
        # The reaper owns its own cancel-and-await; the warm-up is a one-shot task with no
        # loop behind it, so it stays hand-rolled rather than being bent into a timer.
        await self._reaper.stop()
        # Drained, never cancelled: a teardown interrupted partway leaves a guest copy
        # half brought out. Drained *before* taking the lock, which is
        # what each of them needs to release its own tombstone.
        if self._teardowns:
            await asyncio.wait(self._teardowns, timeout=_SESSION_STOP_TIMEOUT_S)
        async with self._lock:
            # In parallel and each bounded: serially, N sessions at up to 30s apiece was a
            # shutdown measured in minutes. A session cut off by its cap is left behind
            # (its guest copy is gone at the next VM boot) rather than allowed to hold the
            # process open.
            async def _stop(session: SandboxSession) -> None:
                try:
                    await asyncio.wait_for(session.shutdown(), _SESSION_STOP_TIMEOUT_S)
                except Exception:  # noqa: BLE001 — tear the rest down regardless
                    logger.warning("sandbox: session teardown did not finish", exc_info=True)

            await asyncio.gather(*(_stop(s) for s in list(self._sessions.values())))
            self._sessions.clear()
            self._preview_tokens.clear()

    async def _sweep(self) -> None:
        now = time.monotonic()
        async with self._lock:
            # A claimed fork is the one thing time may not collect. Reaping an ordinary
            # session is lossless — the guest copy goes, the files stay — which is why the
            # sweep otherwise ignores who holds what, down to a run parked on an
            # unanswered approval. Reaping a fork *deletes* it.
            stale = [
                key
                for key, s in self._sessions.items()
                if not s.is_busy
                and s.idle_seconds(now) >= self._idle_ttl
                and not (s.ephemeral and s.is_claimed)
            ]
            # Snapshot + detach under the lock (so a concurrent acquire() can't
            # mint a second session onto the same workspace mid-teardown), but the
            # teardown itself (several VM round trips, potentially slow) runs
            # OUTSIDE the lock, a few at a time — a mass reap must not stall unrelated
            # conversations' acquire()/start_preview()/purge() for the sum of all of
            # them (sandbox-02). A per-key tombstone in `_tearing_down` lets that *same*
            # key's acquire/purge wait for its own teardown specifically, never anyone
            # else's.
            detached = self._detach(stale)

        await self._tear_down(detached)
        await self._collect_orphan_forks()

    async def _collect_orphan_forks(self) -> None:
        """Delete the delegated-agent forks an unclean shutdown stranded.

        *Which* directories those are belongs to
        :func:`~services.sandbox.fork.orphan_fork_keys`, with the rest of what a
        dead process leaves behind. What is here is the deleting, and it goes through a
        throwaway session and the ordinary detach path rather than a bare ``rmtree`` so
        it inherits every protection that path carries: the tombstone protocol, so an
        ``acquire()`` for that key arriving mid-delete waits instead of minting a
        session onto the directory being removed; and that same tombstone's release in a
        ``finally``, however the leg ends.

        A directory name is all this path has, and the conversation key it was derived
        from cannot be recovered from it — so these sessions get the allowlist directory
        that *name* maps to (``dir_for``, not ``allow_dir``, which would encode an
        already-encoded name). Nothing reads it: an allowlist is read for a running
        command, and this path runs none."""
        orphans: list[_Detached] = []
        async with self._lock:
            # Listed under the lock, so the two maps it reads cannot shift underneath
            # the answer — which is the whole reason the key discovery takes them as an
            # argument rather than reaching for them itself.
            taken = self._sessions.keys() | self._tearing_down.keys()
            for safe in orphan_fork_keys(self._work_root, taken)[:_ORPHAN_FORKS_PER_SWEEP]:
                event = asyncio.Event()
                self._tearing_down[safe] = event
                session = self._new_session(safe, self._egress.dir_for(safe), ephemeral=True)
                orphans.append((safe, session, event))
        if not orphans:
            return
        logger.info(
            "sandbox: collecting %d delegated fork(s) left behind by an unclean shutdown",
            len(orphans),
        )
        await self._tear_down(orphans)
