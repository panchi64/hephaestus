"""The attention surface's substrate — an in-memory index + ring-buffered live stream,
backed by a lock-aware write-behind drainer for durability.

Mirrors two existing disciplines rather than inventing a third:

- **`runs/stream.py`'s live-fan-out discipline**, for the stream half: `_emit` is
  synchronous and `subscribe` registers-then-snapshots with no `await` between, so a
  subscriber can never observe a gap or a duplicate. Unlike a `RunStream` (buffered for
  one run's lifetime, then closed), this stream is process-lifetime and its buffer is a
  true ring — bounded, oldest evicted — since it isn't scoped to one short-lived run.
- **`services/conversations.py`'s memory-fast/DB-durable split**, for the record half:
  an in-memory index is the authoritative read path (what `list_notifications`,
  `mark_read`, etc. answer against, immediately and consistently), while a background
  `WriteBehindWorker` persists it off the critical path, encrypting the sealed fields
  only in the drainer (the lock-aware side of the queue) so a locked vault parks the
  write instead of losing it. Read/resolve mutations ride the **same** drainer queue as
  their creating insert, so an update can never race ahead of — and silently no-op
  against — a row that hasn't landed in the DB yet.

This module only records and streams what it's told. *Whether* to notify (the emit
policy: which run outcomes are noteworthy, whether anyone was already watching) lives
with the callers — the engine, the run registry, the approval routes — not here.

**Out-of-band delivery** (`AE-3.2`, `TASK-6`) rides on top: the in-app record is always
written, and any :mod:`services.notification_channels` channel handed in additionally
carries the interruption-worthy kinds to email/push. Delivery uses a second lock-aware
drainer, so a channel is never on the critical path of the thing that notified: an
unreachable mail server degrades the run to in-app-only rather than breaking its approval
park, and a locked vault parks the send (the sealed address can't be opened) rather than
losing it.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import AsyncIterator, Callable, Iterable
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Literal

from sqlalchemy import Engine
from sqlmodel import Session, select

from core.db import in_session
from core.serde import as_utc
from core.shutdown import STOPPED, get_unless_shutdown
from core.tasks import STOP_TIMEOUT_S, cancel_and_wait
from core.vault import Vault
from core.worker import WriteBehindWorker
from models._fields import new_id, utcnow
from models.notification import Notification
from services.notification_channels import (
    OUT_OF_BAND_KINDS,
    ChannelHealth,
    NotificationChannel,
)
from services.notification_view import NotificationView

logger = logging.getLogger(__name__)

# Per-subscriber backlog cap before a stalled consumer is dropped (mirrors
# `runs/stream.py`'s `SUBSCRIBER_QUEUE_MAX`).
SUBSCRIBER_QUEUE_MAX = 1024

# The stream's ring-buffer depth. Unlike a per-run `RunStream` (buffered for the run's
# whole lifetime), this surface lives as long as the process — a true ring keeps replay
# bounded; a reconnect past the horizon simply misses the oldest events (the same
# in-memory-only tradeoff `runs/stream.py` already licenses).
_RING_BUFFER_MAX = 500


@dataclass(frozen=True, slots=True)
class NotificationEvent:
    """One stamped stream event — this surface's own seq/ts, mirroring `runs.Event`
    but carrying a nested `notification` payload rather than a flat body (the wire
    envelope needs the full current view, not just what changed)."""

    seq: int
    ts: datetime
    kind: Literal["created", "updated", "deleted"]
    notification: NotificationView


@dataclass
class _Job:
    """A unit of durable work. ``create`` inserts a fresh row (encrypting title/body
    just before the write); ``update`` writes back the current read/resolved state —
    always the *whole* current state, never a delta, so replaying it is idempotent
    even if two updates for the same id end up queued back to back. ``delete`` removes
    the rows named in ``ids`` in one transaction; it rides the same FIFO queue, so it lands
    after any pending create/update."""

    op: Literal["create", "update", "delete"]
    id: str = ""
    ids: tuple[str, ...] = ()
    owner_id: str = ""
    notif_kind: str = ""
    title: str = ""
    body: str | None = None
    conversation_id: str | None = None
    run_id: str | None = None
    task_id: str | None = None
    created_at: datetime | None = None
    read_at: datetime | None = None
    resolved_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class _Delivery:
    """One notification, queued for one channel. Deliberately **one job per channel**
    rather than one per notification: a retry then re-runs only the channel that failed,
    so a flaky push endpoint can never re-send an email the operator already got
    (`TASK-6` forbids duplicates)."""

    channel: NotificationChannel
    view: NotificationView


class NotificationService:
    def __init__(
        self,
        engine: Engine,
        vault: Vault,
        *,
        channels: list[NotificationChannel] | None = None,
        ring_buffer_max: int = _RING_BUFFER_MAX,
    ) -> None:
        self._engine: Engine = engine
        self._vault: Vault = vault
        self._channels: list[NotificationChannel] = channels or []
        self._cache: dict[str, NotificationView] = {}
        # Deleted ids whose DB delete is still queued; keeps `rehydrate` from re-reading them.
        self._pending_deletes: set[str] = set()
        self._buffer: deque[NotificationEvent] = deque(maxlen=ring_buffer_max)
        self._seq: int = 0
        self._subscribers: set[asyncio.Queue[NotificationEvent | None]] = set()
        self._worker: WriteBehindWorker[_Job] = WriteBehindWorker(
            self._persist,
            name="notifications-drainer",
            unlocked=vault.unlocked_event,
            on_drop=self._on_drop,
        )
        # Separate from the persistence drainer on purpose: a mail server that is slow or
        # down must not hold up the durable write of the record itself, and the two have
        # different failure meanings (a lost row is data loss; a lost send is a missed
        # ping). Lock-aware because the channels' addresses are vault-sealed.
        self._deliveries: WriteBehindWorker[_Delivery] = WriteBehindWorker(
            self._deliver,
            name="notifications-channels",
            unlocked=vault.unlocked_event,
            on_drop=self._on_delivery_drop,
        )
        self._rehydrate_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        await self._worker.start()
        await self._deliveries.start()
        # Best-effort, off the critical path: once unlocked, lift prior history into
        # the cache so a restart's REST/unread state reflects it, not just what's
        # notified after this boot (mirrors the embedding backfill's posture).
        self._rehydrate_task = asyncio.create_task(self.rehydrate(), name="notifications-rehydrate")

    async def flush(self) -> None:
        """Wait until the write-behind queue has drained."""
        await self._worker.join()

    def evict_cached(self, notification_id: str) -> None:
        """Drop one notification from the in-memory cache; the database row stays."""
        _ = self._cache.pop(notification_id)

    async def stop(self) -> None:
        await cancel_and_wait(self._rehydrate_task, STOP_TIMEOUT_S)
        await self._deliveries.stop()
        await self._worker.stop()
        # Channels last: the delivery drainer above may still have been using one. A
        # channel that owns an outbound client (push) closes it here; one that borrows
        # everything (email) is a no-op. Failing to close must not abort a shutdown that
        # still has stores to flush below it.
        for channel in self._channels:
            try:
                await channel.close()
            except Exception:
                logger.exception("notifications: closing the %s channel failed", channel.key)

    @property
    def last_seq(self) -> int:
        return self._seq

    @property
    def channels(self) -> list[NotificationChannel]:
        """The delivery channels notifications fan out to."""
        return self._channels

    @channels.setter
    def channels(self, value: list[NotificationChannel]) -> None:
        self._channels = value

    @property
    def subscriber_count(self) -> int:
        """How many live subscribers are attached right now."""
        return len(self._subscribers)

    @property
    def rehydrate_task(self) -> asyncio.Task[None] | None:
        """The startup history-rehydration task, or None before `start`."""
        return self._rehydrate_task

    async def flush_deliveries(self) -> None:
        """Wait until every queued channel delivery has been attempted."""
        await self._deliveries.join()

    async def channel_health(self, owner_id: str) -> list[ChannelHealth]:
        """Every configured channel's health, for the operator's status surface
        (`XC-DEG-3`). Never raises — each channel reports its own state."""
        return [await channel.health(owner_id) for channel in self._channels]

    # --- writes -------------------------------------------------------------

    async def notify(
        self,
        owner_id: str,
        kind: str,
        title: str,
        body: str | None = None,
        *,
        conversation_id: str | None = None,
        run_id: str | None = None,
        task_id: str | None = None,
    ) -> NotificationView:
        """Record a new notification: cached + streamed immediately, persisted off the
        critical path."""
        view = NotificationView(
            id=new_id(),
            owner_id=owner_id,
            kind=kind,
            title=title,
            body=body,
            conversation_id=conversation_id,
            run_id=run_id,
            task_id=task_id,
            created_at=utcnow(),
            read_at=None,
            resolved_at=None,
        )
        self._cache[view.id] = view
        _ = self._emit("created", view)
        self._worker.submit(
            _Job(
                op="create",
                id=view.id,
                owner_id=owner_id,
                notif_kind=kind,
                title=title,
                body=body,
                conversation_id=conversation_id,
                run_id=run_id,
                task_id=task_id,
                created_at=view.created_at,
            )
        )
        self._dispatch(view)
        return view

    async def mark_read(self, owner_id: str, ids: list[str]) -> int:
        """Mark specific notifications read. Ids that don't exist, aren't the owner's,
        or are already read are silently skipped — idempotent, never an error. Returns
        how many actually changed."""
        return self._set_read(owner_id, ids, utcnow())

    async def mark_all_read(self, owner_id: str) -> int:
        """Mark every one of the owner's unread notifications read. Returns how many
        changed."""
        return self._set_read(owner_id, None, utcnow())

    async def mark_unread(self, owner_id: str, ids: list[str]) -> int:
        """Mark specific read notifications unread. Unknown, unowned, or already-unread
        ids are skipped. Returns how many changed."""
        return self._set_read(owner_id, ids, None)

    async def delete(self, owner_id: str, ids: list[str]) -> int:
        """Delete specific notifications. Unknown or unowned ids are skipped. Returns
        how many were removed."""
        wanted = set(ids)
        return self._delete(owner_id, lambda view: view.id in wanted)

    async def delete_read(self, owner_id: str) -> int:
        """Delete every one of the owner's read notifications. Returns how many were
        removed."""
        return self._delete(owner_id, lambda view: view.read_at is not None)

    async def delete_all(self, owner_id: str) -> int:
        """Delete every one of the owner's notifications. Returns how many were removed."""
        return self._delete(owner_id, lambda _view: True)

    async def delete_for_conversations(self, owner_id: str, conversation_ids: set[str]) -> int:
        """Delete the owner's notifications about these conversations, which are gone and
        can no longer be opened. Returns how many were removed."""
        return self._delete(owner_id, lambda view: view.conversation_id in conversation_ids)

    async def resolve_for_run(self, owner_id: str, run_id: str) -> list[NotificationView]:
        """Resolve every one of the run's not-yet-resolved notifications (in practice
        at most its one `approval_needed`) — the approval was decided by some path, or
        the run reached terminal without one ever being decided. A resolved notification
        needs no attention, so it is marked read too. Returns the resolved views (empty
        if the run had none pending — idempotent)."""
        now = utcnow()
        resolved: list[NotificationView] = []
        for notif_id, view in list(self._cache.items()):
            if view.owner_id != owner_id or view.run_id != run_id or view.resolved_at is not None:
                continue
            updated = replace(view, resolved_at=now, read_at=view.read_at or now)
            self._cache[notif_id] = updated
            resolved.append(updated)
        for view in resolved:
            _ = self._emit("updated", view)
            self._queue_update(view)
        return resolved

    # --- reads ----------------------------------------------------------------

    async def list_notifications(
        self,
        owner_id: str,
        *,
        limit: int = 50,
        before: datetime | None = None,
        unread_only: bool = False,
    ) -> tuple[list[NotificationView], int]:
        """Newest-first page of the owner's notifications, plus their current unread
        count. Reads the in-memory index — the same authoritative state `notify` /
        `mark_read` / etc. just updated — rather than a DB round-trip that could lag
        behind a write still sitting in the drainer's queue."""
        # `before` arrives from a query string, so it is aware only when the client
        # sent an offset — normalize it onto the same scale as the cached values.
        before = as_utc(before)
        owned = [v for v in self._cache.values() if v.owner_id == owner_id]
        unread_count = sum(1 for v in owned if v.read_at is None)
        if unread_only:
            owned = [v for v in owned if v.read_at is None]
        if before is not None:
            owned = [v for v in owned if v.created_at < before]
        owned.sort(key=lambda v: v.created_at, reverse=True)
        return owned[:limit], unread_count

    async def subscribe(self, after_seq: int = 0) -> AsyncIterator[NotificationEvent]:
        """Replay ring-buffered events after ``after_seq`` then stream live ones. Never
        ends on its own — this surface is process-lifetime, not tied to one run's
        terminal state — so iteration only stops when the caller does (a client
        disconnect cancelling the pump task, in the SSE route) or the app begins shutting down."""
        queue: asyncio.Queue[NotificationEvent | None] = asyncio.Queue(maxsize=SUBSCRIBER_QUEUE_MAX)
        # --- atomic: register before snapshot, no await between ---
        self._subscribers.add(queue)
        backlog = (
            [e for e in self._buffer if e.seq > after_seq] if after_seq > 0 else list(self._buffer)
        )
        # ------------------------------------------------------------
        try:
            for event in backlog:
                yield event
            while True:
                event = await get_unless_shutdown(queue)
                if event is None or event is STOPPED:  # evicted as slow, or the app is stopping
                    return
                yield event
        finally:
            self._subscribers.discard(queue)

    # --- internals --------------------------------------------------------

    def _set_read(self, owner_id: str, ids: Iterable[str] | None, read_at: datetime | None) -> int:
        """Move the owner's notifications to ``read_at`` (``None`` means unread). ``ids``
        of ``None`` covers every cached row; rows already in that state are skipped."""
        if ids is None:
            candidates = list(self._cache.values())
        else:
            candidates = [v for i in set(ids) if (v := self._cache.get(i)) is not None]
        changed: list[NotificationView] = []
        for view in candidates:
            if view.owner_id != owner_id or (view.read_at is None) == (read_at is None):
                continue
            updated = replace(view, read_at=read_at)
            self._cache[view.id] = updated
            changed.append(updated)
        for view in changed:
            _ = self._emit("updated", view)
            self._queue_update(view)
        return len(changed)

    def _delete(self, owner_id: str, predicate: Callable[[NotificationView], bool]) -> int:
        removed = [
            view for view in self._cache.values() if view.owner_id == owner_id and predicate(view)
        ]
        for view in removed:
            del self._cache[view.id]
            self._pending_deletes.add(view.id)
            _ = self._emit("deleted", view)
        if removed:
            self._worker.submit(_Job(op="delete", ids=tuple(view.id for view in removed)))
        return len(removed)

    def _emit(
        self, kind: Literal["created", "updated", "deleted"], view: NotificationView
    ) -> NotificationEvent:
        """Stamp, ring-buffer, and fan out. Synchronous and atomic — no `await` between
        a mutation landing in the cache and this fan-out, so `subscribe` can never
        observe a state between the two (mirrors `RunStream.emit`)."""
        self._seq += 1
        event = NotificationEvent(seq=self._seq, ts=utcnow(), kind=kind, notification=view)
        self._buffer.append(event)  # a bounded deque evicts the oldest on overflow
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                self._drop_slow(queue)
        return event

    def _drop_slow(self, queue: asyncio.Queue[NotificationEvent | None]) -> None:
        """Evict a subscriber that isn't keeping up — it can reconnect and replay from
        the ring buffer (mirrors `RunStream._drop_slow`)."""
        self._subscribers.discard(queue)
        while not queue.empty():
            _ = queue.get_nowait()
        queue.put_nowait(None)

    def _queue_update(self, view: NotificationView) -> None:
        self._worker.submit(
            _Job(op="update", id=view.id, read_at=view.read_at, resolved_at=view.resolved_at)
        )

    def _on_drop(self, job: _Job, exc: Exception) -> None:
        # A dropped delete keeps its ids in `_pending_deletes`, so `rehydrate` can't
        # bring the rows back for the rest of this process.
        logger.error(
            "notifications: dropped a %s write for %s after retries: %s",
            job.op,
            ", ".join(job.ids) if job.op == "delete" else job.id,
            exc,
        )

    # --- out-of-band delivery (AE-3.2, TASK-6) ------------------------------

    def _dispatch(self, view: NotificationView) -> None:
        """Queue the interruption-worthy kinds to every channel. Synchronous and
        non-blocking, like the persistence submit beside it — the caller that notified
        (an approval park, a reminder fire) never waits on a network."""
        if view.kind not in OUT_OF_BAND_KINDS:
            return
        for channel in self._channels:
            self._deliveries.submit(_Delivery(channel=channel, view=view))

    async def _deliver(self, delivery: _Delivery) -> None:
        await delivery.channel.deliver(delivery.view)

    def _on_delivery_drop(self, delivery: _Delivery, exc: Exception) -> None:
        """A channel that gave up. Logged loudly rather than swallowed — the operator's
        in-app record still exists, so this is a degraded reach, not a lost notification
        (`XC-DEG-3`)."""
        logger.error(
            "notifications: could not reach the operator over %s for %s after retries: %s",
            delivery.channel.key,
            delivery.view.id,
            exc,
        )

    async def rehydrate(self) -> None:
        _ = await self._vault.unlocked_event.wait()
        try:
            rows = await in_session(
                self._engine, lambda session: session.exec(select(Notification)).all()
            )
        except Exception:
            logger.exception("notifications: rehydrate from DB failed")
            return
        for row in rows:
            if row.id in self._pending_deletes:
                continue
            if row.id in self._cache:  # a notify() since boot already cached it fresher
                continue
            self._cache[row.id] = self._row_to_view(row)

    def _row_to_view(self, row: Notification) -> NotificationView:
        return NotificationView(
            id=row.id,
            owner_id=row.owner_id,
            kind=row.kind,
            title=self._vault.decrypt_str(row.title_enc),
            body=self._vault.decrypt_str(row.body_enc) if row.body_enc is not None else None,
            conversation_id=row.conversation_id,
            run_id=row.run_id,
            task_id=row.task_id,
            # SQLite hands these back naive; the cache is also filled by `notify()`
            # with aware ones, so without this the two populations can't be compared
            # or sorted against each other (`list_notifications`).
            created_at=as_utc(row.created_at),
            read_at=as_utc(row.read_at),
            resolved_at=as_utc(row.resolved_at),
        )

    async def _persist(self, job: _Job) -> None:
        if job.op == "create":
            created_at = job.created_at
            assert created_at is not None

            def work(session: Session) -> None:
                session.add(
                    Notification(
                        id=job.id,
                        owner_id=job.owner_id,
                        kind=job.notif_kind,
                        title_enc=self._vault.encrypt_str(job.title),
                        body_enc=(
                            self._vault.encrypt_str(job.body) if job.body is not None else None
                        ),
                        conversation_id=job.conversation_id,
                        run_id=job.run_id,
                        task_id=job.task_id,
                        created_at=created_at,
                    )
                )

            await in_session(self._engine, work)
        elif job.op == "delete":

            def work(session: Session) -> None:
                for row_id in job.ids:
                    row = session.get(Notification, row_id)
                    if row is not None:
                        session.delete(row)

            await in_session(self._engine, work)
            self._pending_deletes.difference_update(job.ids)
        else:

            def work(session: Session) -> None:
                row = session.get(Notification, job.id)
                if row is None:  # the create hasn't landed (shouldn't happen — the
                    return  # single-queue FIFO orders it first) — never resurrect it.
                row.read_at = job.read_at
                row.resolved_at = job.resolved_at
                session.add(row)

            await in_session(self._engine, work)
