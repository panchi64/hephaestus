"""NotificationService: seq stamping, ring-buffer replay/bounds, live fan-out, read/
resolve transitions, and the lock-aware drainer."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import suppress
from datetime import timedelta
from pathlib import Path

from sqlmodel import select

from core.db import in_session, init_db, make_engine
from core.shutdown import begin_shutdown, reset_shutdown
from core.vault import Vault
from models.notification import Notification
from services.notifications import NotificationEvent, NotificationService

OWNER = "operator"


async def _service(tmp_path: Path, *, ring_buffer_max: int | None = None) -> NotificationService:
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    vault = Vault(tmp_path / "keyfile.json")
    await vault.setup("pw")
    if ring_buffer_max is None:
        service = NotificationService(engine, vault)
    else:
        service = NotificationService(engine, vault, ring_buffer_max=ring_buffer_max)
    await service.start()
    return service


def _titles(events: list[NotificationEvent]) -> list[str]:
    return [e.notification.title for e in events]


async def _collect(
    service: NotificationService, *, after_seq: int, n: int
) -> list[NotificationEvent]:
    """Pull exactly ``n`` events off `subscribe`, then close it cleanly — `subscribe`
    never ends on its own (this surface is process-lifetime, not tied to a run)."""
    gen = service.subscribe(after_seq)
    assert isinstance(gen, AsyncGenerator)
    out: list[NotificationEvent] = []
    try:
        for _ in range(n):
            out.append(await gen.__anext__())
    finally:
        await gen.aclose()
    return out


# --- seq + backlog replay -----------------------------------------------------


async def test_notify_assigns_monotonic_seq(tmp_path: Path):
    service = await _service(tmp_path)
    a = await service.notify(OWNER, "system", "a")
    b = await service.notify(OWNER, "system", "b")
    events = await _collect(service, after_seq=0, n=2)
    assert [e.notification.id for e in events] == [a.id, b.id]
    assert [e.seq for e in events] == [1, 2]
    assert service.last_seq == 2
    await service.stop()


async def test_subscribe_resumes_only_after_last_seen_seq(tmp_path: Path):
    service = await _service(tmp_path)
    for t in ("a", "b", "c"):
        _ = await service.notify(OWNER, "system", t)
    events = await _collect(service, after_seq=1, n=2)
    assert _titles(events) == ["b", "c"]
    assert [e.seq for e in events] == [2, 3]
    await service.stop()


async def test_live_subscriber_receives_new_notifications(tmp_path: Path):
    service = await _service(tmp_path)
    received: list[NotificationEvent] = []

    async def consume():
        async for event in service.subscribe():
            received.append(event)
            return  # just the first live event

    task = asyncio.create_task(consume())
    await asyncio.sleep(0)  # let it register
    _ = await service.notify(OWNER, "system", "hello")
    await task
    assert _titles(received) == ["hello"]
    await service.stop()


async def test_fanout_to_multiple_subscribers(tmp_path: Path):
    service = await _service(tmp_path)
    out_a: list[NotificationEvent] = []
    out_b: list[NotificationEvent] = []

    async def consume(sink: list[NotificationEvent]) -> None:
        async for event in service.subscribe():
            sink.append(event)
            return

    tasks = [asyncio.create_task(consume(out_a)), asyncio.create_task(consume(out_b))]
    await asyncio.sleep(0)
    _ = await service.notify(OWNER, "system", "x")
    _ = await asyncio.gather(*tasks)
    assert _titles(out_a) == ["x"]
    assert _titles(out_b) == ["x"]
    await service.stop()


# --- ring-buffer bounds --------------------------------------------------------


async def test_ring_buffer_evicts_oldest_beyond_cap(tmp_path: Path):
    service = await _service(tmp_path, ring_buffer_max=3)
    for i in range(5):
        _ = await service.notify(OWNER, "system", f"n{i}")
    events = await _collect(service, after_seq=0, n=3)
    # Only the 3 most recent survive the ring; the seq numbering still reflects the
    # true (evicted) history, so a very-stale Last-Event-ID just can't be replayed.
    assert _titles(events) == ["n2", "n3", "n4"]
    assert [e.seq for e in events] == [3, 4, 5]
    await service.stop()


async def test_slow_subscriber_is_dropped_not_grown_unbounded(tmp_path: Path):
    from services.notifications import _SUBSCRIBER_QUEUE_MAX

    service = await _service(tmp_path)

    async def stalled():  # registers, then never reads
        async for _ in service.subscribe():
            await asyncio.sleep(3600)

    task = asyncio.create_task(stalled())
    await asyncio.sleep(0)
    assert len(service._subscribers) == 1

    for i in range(_SUBSCRIBER_QUEUE_MAX + 50):  # flood past the cap (no yields)
        _ = await service.notify(OWNER, "system", str(i))

    assert len(service._subscribers) == 0  # evicted, not ballooning
    _ = task.cancel()
    with suppress(asyncio.CancelledError):
        await task
    await service.stop()


# --- read / resolve transitions -------------------------------------------------


async def test_mark_read_flags_and_reports_count(tmp_path: Path):
    service = await _service(tmp_path)
    a = await service.notify(OWNER, "system", "a")
    b = await service.notify(OWNER, "system", "b")

    assert await service.mark_read(OWNER, [a.id]) == 1
    items, unread = await service.list_notifications(OWNER)
    by_id = {v.id: v for v in items}
    assert by_id[a.id].read_at is not None
    assert by_id[b.id].read_at is None
    assert unread == 1

    # Idempotent: re-marking an already-read id (or an unknown one) changes nothing.
    assert await service.mark_read(OWNER, [a.id, "no-such-id"]) == 0
    await service.stop()


async def test_mark_read_emits_notification_updated_live(tmp_path: Path):
    service = await _service(tmp_path)
    view = await service.notify(OWNER, "system", "a")
    received: list[NotificationEvent] = []

    async def consume():
        async for event in service.subscribe(after_seq=service.last_seq):
            received.append(event)
            return

    task = asyncio.create_task(consume())
    await asyncio.sleep(0)
    _ = await service.mark_read(OWNER, [view.id])
    await task

    assert received[0].kind == "updated"
    assert received[0].notification.id == view.id
    assert received[0].notification.read_at is not None
    await service.stop()


async def test_mark_all_read_flags_every_unread(tmp_path: Path):
    service = await _service(tmp_path)
    _ = await service.notify(OWNER, "system", "a")
    _ = await service.notify(OWNER, "system", "b")

    assert await service.mark_all_read(OWNER) == 2
    _, unread = await service.list_notifications(OWNER)
    assert unread == 0
    assert await service.mark_all_read(OWNER) == 0  # idempotent
    await service.stop()


async def test_resolve_for_run_marks_only_that_runs_pending_notification(tmp_path: Path):
    service = await _service(tmp_path)
    approval = await service.notify(OWNER, "approval_needed", "needs approval", run_id="run-1")
    other = await service.notify(OWNER, "run_failed", "unrelated", run_id="run-2")

    resolved = await service.resolve_for_run(OWNER, "run-1")
    assert [r.id for r in resolved] == [approval.id]

    items, _ = await service.list_notifications(OWNER)
    by_id = {v.id: v for v in items}
    assert by_id[approval.id].resolved_at is not None
    assert by_id[other.id].resolved_at is None

    assert await service.resolve_for_run(OWNER, "run-1") == []  # idempotent
    await service.stop()


# --- durability: the lock-aware drainer ----------------------------------------


async def test_drainer_parks_while_locked_and_drains_after_unlock(tmp_path: Path):
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    vault = Vault(tmp_path / "keyfile.json")
    await vault.setup("pw")
    service = NotificationService(engine, vault)
    await service.start()

    vault.lock()
    view = await service.notify(OWNER, "system", "locked write")

    # Cached + streamed immediately regardless of lock state — only the durable
    # write is gated. Read straight from the DB: nothing has landed yet.
    def rows():
        return in_session(engine, lambda session: session.exec(select(Notification)).all())

    assert await rows() == []

    _ = await vault.unlock("pw")
    await service.flush()  # let the parked write drain now that it can encrypt
    await service.stop()

    persisted = await rows()
    assert len(persisted) == 1
    assert persisted[0].id == view.id
    assert "locked write" not in persisted[0].title_enc  # sealed at rest


async def test_cold_restart_rehydrates_prior_notifications(tmp_path: Path):
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    vault = Vault(tmp_path / "keyfile.json")
    await vault.setup("pw")

    service = NotificationService(engine, vault)
    await service.start()
    view = await service.notify(OWNER, "system", "before restart", "with a body")
    await service.flush()
    await service.stop()

    cold = NotificationService(engine, vault)
    await cold.start()
    assert cold._rehydrate_task is not None
    await cold._rehydrate_task  # deterministic in tests: vault is already unlocked
    items, _ = await cold.list_notifications(OWNER)
    assert [i.id for i in items] == [view.id]
    assert items[0].title == "before restart"
    assert items[0].body == "with a body"
    await cold.stop()


async def test_rehydrated_and_live_notifications_sort_together(tmp_path: Path):
    """The cache has two populations after a restart — rows read back from SQLite (which
    has no tz type, so they arrive naive) and anything `notify()` cached since boot (aware
    from `utcnow()`). Listing them compares and sorts across both, so the read must
    normalize or it raises `TypeError: can't compare offset-naive and offset-aware`."""
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    vault = Vault(tmp_path / "keyfile.json")
    await vault.setup("pw")

    service = NotificationService(engine, vault)
    await service.start()
    old = await service.notify(OWNER, "system", "persisted")
    await service.flush()
    await service.stop()

    # SQLModel now declares a datetime field as its own UTC-restoring column type, so the
    # round-trip is no longer lossy through the ORM — the normalization on read is what
    # still covers a row written before that, and this pins that the two populations sort.
    persisted = await in_session(engine, lambda session: session.exec(select(Notification)).all())
    assert persisted[0].created_at.utcoffset() in (None, timedelta(0))

    cold = NotificationService(engine, vault)
    await cold.start()
    assert cold._rehydrate_task is not None
    await cold._rehydrate_task
    fresh = await cold.notify(OWNER, "system", "live")

    items, unread = await cold.list_notifications(OWNER)
    assert [i.id for i in items] == [fresh.id, old.id]  # newest first, across both
    assert unread == 2
    assert all(i.created_at.tzinfo is not None for i in items)

    # `before` filters across both populations too, offset or not.
    aware = fresh.created_at
    for cutoff in (aware, aware.replace(tzinfo=None)):
        page, _ = await cold.list_notifications(OWNER, before=cutoff)
        assert [i.id for i in page] == [old.id]
    await cold.stop()


async def test_subscribe_ends_when_the_app_begins_shutting_down(tmp_path: Path):
    """The feed has no end of its own; the shutdown signal is what lets the server stop."""
    service = await _service(tmp_path)
    try:
        ended = asyncio.Event()

        async def consume() -> None:
            async for _ in service.subscribe(0):
                pass
            ended.set()

        task = asyncio.create_task(consume())
        await asyncio.sleep(0.05)
        assert not ended.is_set()
        begin_shutdown()
        _ = await asyncio.wait_for(ended.wait(), 1.0)
        await task
    finally:
        reset_shutdown()
        await service.stop()
