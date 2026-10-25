"""`park_for_input`/`approval_conversation_title` in isolation: title wording,
grant-covered filtering, conversation-title lookup, holding the notice while the chat is
watched, and the "a notifier failure must never break the park" guarantee — without
driving a full model turn."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import override

from pydantic_ai import Agent, DeferredToolRequests, ToolApproved, ToolCallPart
from pydantic_ai.models.test import TestModel

from agent.park_notice import ParkNotices
from agent.parking import ParkedTurn, ParkNotifier, park_for_input
from core.db import init_db, make_engine
from core.vault import Vault
from runs.run import Run, RunStatus
from runs.stream import RunStream
from services.conversations import ConversationStore
from services.notification_view import NotificationView
from services.notifications import NotificationService

from ._helpers import leave_stream, watch_stream

OWNER = "operator"


def _run(conversation_id: str | None = "c1") -> Run:
    return Run(
        id="run-1", kind="chat", owner_id=OWNER, conversation_id=conversation_id, stream=RunStream()
    )


_APPROVALS = [
    ToolCallPart("danger.delete_thing", {}, tool_call_id="call-1"),
    ToolCallPart("danger.wipe_all", {}, tool_call_id="call-2"),
]
_AGENT = Agent(TestModel(), deps_type=object)


def _requests() -> DeferredToolRequests:
    return DeferredToolRequests(approvals=list(_APPROVALS))


async def _notification_service(tmp_path: Path) -> NotificationService:
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    vault = Vault(tmp_path / "keyfile.json")
    await vault.setup("pw")
    service = NotificationService(engine, vault)
    await service.start()
    return service


async def test_park_names_conversation_and_pending_tools(tmp_path: Path):
    service = await _notification_service(tmp_path)
    run = _run()

    await park_for_input(
        run,
        _AGENT,
        [],
        _requests(),
        set(),
        notifications=service,
        store=None,
        conversation_id="c1",
    )

    assert run.status is RunStatus.awaiting_input
    items, unread = await service.list_notifications(OWNER)
    assert unread == 1
    assert items[0].kind == "approval_needed"
    assert items[0].run_id == "run-1"
    assert items[0].conversation_id == "c1"
    assert "danger.delete_thing" in items[0].title
    assert "danger.wipe_all" in items[0].title
    await service.stop()


async def test_park_excludes_grant_covered_tool_names(tmp_path: Path):
    service = await _notification_service(tmp_path)
    run = _run()

    await park_for_input(
        run,
        _AGENT,
        [],
        _requests(),
        set(),
        settled={"call-1": ToolApproved()},
        notifications=service,
        store=None,
        conversation_id="c1",
    )

    items, _ = await service.list_notifications(OWNER)
    assert len(items) == 1
    assert "danger.delete_thing" not in items[0].title
    assert "danger.wipe_all" in items[0].title
    await service.stop()


async def test_park_skips_notifying_when_every_call_is_grant_covered(tmp_path: Path):
    service = await _notification_service(tmp_path)
    run = _run()

    await park_for_input(
        run,
        _AGENT,
        [],
        _requests(),
        set(),
        settled={"call-1": ToolApproved(), "call-2": ToolApproved()},
        notifications=service,
        store=None,
        conversation_id="c1",
    )

    assert run.status is RunStatus.awaiting_input  # still parks — just doesn't notify
    items, unread = await service.list_notifications(OWNER)
    assert items == []
    assert unread == 0
    await service.stop()


async def test_park_is_a_no_op_notify_when_notifications_is_none():
    run = _run()
    # Must not raise — a run without a wired notifier simply doesn't notify.
    await park_for_input(
        run, _AGENT, [], _requests(), set(), notifications=None, conversation_id="c1"
    )
    assert run.status is RunStatus.awaiting_input


async def test_park_uses_the_conversation_title_when_one_exists(tmp_path: Path):
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    vault = Vault(tmp_path / "keyfile2.json")
    await vault.setup("pw")
    store = ConversationStore(engine, vault)
    await store.start()
    conv_id = await store.create_conversation(OWNER)
    await store.set_title(conv_id, "Refactor the widget")

    service = await _notification_service(tmp_path)
    run = _run(conversation_id=conv_id)

    await park_for_input(
        run,
        _AGENT,
        [],
        _requests(),
        set(),
        notifications=service,
        store=store,
        conversation_id=conv_id,
    )

    items, _ = await service.list_notifications(OWNER)
    assert "Refactor the widget" in items[0].title
    await store.stop()
    await service.stop()


async def test_park_falls_back_to_a_generic_label_without_a_title(tmp_path: Path):
    service = await _notification_service(tmp_path)
    run = _run()

    # No store at all — title lookup degrades to the generic fallback rather than
    # failing the notify.
    await park_for_input(
        run,
        _AGENT,
        [],
        _requests(),
        set(),
        notifications=service,
        store=None,
        conversation_id="c1",
    )

    items, _ = await service.list_notifications(OWNER)
    assert "this conversation" in items[0].title
    await service.stop()


async def test_a_raising_notifier_never_breaks_the_park():
    class _RaisingNotifier(ParkNotifier):
        @override
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
            raise RuntimeError("boom")

    run = _run()
    # Must not raise — the parked run's own outcome must survive a notify failure.
    await park_for_input(
        run,
        _AGENT,
        [],
        _requests(),
        set(),
        notifications=_RaisingNotifier(),
        store=None,
        conversation_id="c1",
    )
    assert run.status is RunStatus.awaiting_input
    assert isinstance(run.parked_payload, ParkedTurn)


class _Grace:
    """A sleep that ends only when the test says that grace period has run out."""

    def __init__(self) -> None:
        self.periods: list[asyncio.Event] = []

    async def sleep(self, _seconds: float) -> None:
        period = asyncio.Event()
        self.periods.append(period)
        _ = await period.wait()

    def elapse(self, index: int = -1) -> None:
        self.periods[index].set()


def _counting_send() -> tuple[list[int], Callable[[], Awaitable[None]]]:
    sent: list[int] = []

    async def send() -> None:
        sent.append(1)

    return sent, send


def _parked(payload: object) -> Run:
    run = _run()
    run.park(payload)
    return run


async def test_a_watched_park_holds_its_notice_until_the_chat_is_left(tmp_path: Path):
    service = await _notification_service(tmp_path)
    grace = _Grace()
    holds = ParkNotices(sleep=grace.sleep)
    run = _run()
    watcher = await watch_stream(run.stream)

    await park_for_input(
        run,
        _AGENT,
        [],
        _requests(),
        set(),
        notifications=service,
        holds=holds,
        store=None,
        conversation_id="c1",
    )
    assert run.status is RunStatus.awaiting_input
    assert (await service.list_notifications(OWNER))[0] == []

    await leave_stream(watcher)
    grace.elapse()
    await holds.flush()
    items, _ = await service.list_notifications(OWNER)
    assert [n.kind for n in items] == ["approval_needed"]
    await service.stop()


async def test_a_tailing_client_does_not_hold_the_notice(tmp_path: Path):
    service = await _notification_service(tmp_path)
    run = _run()
    tail = await watch_stream(run.stream, watching=False)

    await park_for_input(
        run,
        _AGENT,
        [],
        _requests(),
        set(),
        notifications=service,
        holds=ParkNotices(),
        store=None,
        conversation_id="c1",
    )

    assert len((await service.list_notifications(OWNER))[0]) == 1
    await leave_stream(tail)
    await service.stop()


async def test_a_held_notice_waits_again_when_the_operator_comes_back():
    grace = _Grace()
    holds = ParkNotices(sleep=grace.sleep)
    payload = object()
    run = _parked(payload)
    sent, send = _counting_send()
    watcher = await watch_stream(run.stream)
    holds.hold(run, payload, send)

    await leave_stream(watcher)
    watcher = await watch_stream(run.stream)
    grace.elapse()
    await holds.flush()
    assert sent == []

    await leave_stream(watcher)
    grace.elapse()
    await holds.flush()
    assert sent == [1]


async def test_the_grace_restarts_at_each_leave():
    grace = _Grace()
    holds = ParkNotices(sleep=grace.sleep)
    payload = object()
    run = _parked(payload)
    sent, send = _counting_send()
    watcher = await watch_stream(run.stream)
    holds.hold(run, payload, send)

    await leave_stream(watcher)
    await asyncio.sleep(0)
    await leave_stream(await watch_stream(run.stream))
    await asyncio.sleep(0)
    grace.elapse(0)
    await asyncio.sleep(0)
    assert sent == []

    grace.elapse(1)
    await holds.flush()
    assert sent == [1]


async def test_a_held_notice_is_dropped_once_the_park_is_settled():
    grace = _Grace()
    holds = ParkNotices(sleep=grace.sleep)
    payload = object()
    run = _parked(payload)
    sent, send = _counting_send()
    watcher = await watch_stream(run.stream)
    holds.hold(run, payload, send)

    run.status = RunStatus.running
    await leave_stream(watcher)
    await holds.flush()
    assert sent == []
    assert grace.periods == []


async def test_a_repark_replaces_the_hold():
    grace = _Grace()
    holds = ParkNotices(sleep=grace.sleep)
    first, second = object(), object()
    run = _parked(first)
    first_sent, first_send = _counting_send()
    second_sent, second_send = _counting_send()
    watcher = await watch_stream(run.stream)
    holds.hold(run, first, first_send)

    run.park(second)
    holds.hold(run, second, second_send)
    await leave_stream(watcher)
    grace.elapse()
    await holds.flush()
    assert (first_sent, second_sent) == ([], [1])


async def test_drain_cancels_a_pending_notice():
    grace = _Grace()
    holds = ParkNotices(sleep=grace.sleep)
    payload = object()
    run = _parked(payload)
    sent, send = _counting_send()
    watcher = await watch_stream(run.stream)
    holds.hold(run, payload, send)
    await leave_stream(watcher)
    await asyncio.sleep(0)

    await holds.drain()
    grace.elapse()
    await asyncio.sleep(0)
    assert sent == []
