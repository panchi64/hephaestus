"""The conversation inbox: nothing sent to a thread is refused or lost.

Every message waits in the thread's inbox until a model request takes it or the operator
withdraws it, and is delivered in hierarchy order: the operator first, then sub-agent
reports, then agent messages, then notices.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import cast

import pytest
from pydantic_ai import ModelMessage, ModelRequest, UserPromptPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from core.db import init_db, make_engine
from core.vault import Vault
from harness.manifests._subagent_wake import SubagentWake
from routes.deps import OPERATOR_ID, app_state
from runs import STOP_NOTICE, ConversationInbox, QueuedMessage, RunStatus, TurnPayload
from runs.events import EventBody, InboxWaiting, MessageInjected, MessageSource
from services.inbox_journal import SealedInboxJournal
from services.registry import ModelRegistry

from ._helpers import client_app, register_stub_provider, settle, stub_resolution
from ._json import arr, json_body, obj, s

_SOURCES: list[MessageSource] = ["operator", "operator", "subagent", "agent", "system"]

# -- the inbox on its own -----------------------------------------------------------------


def _inbox() -> tuple[ConversationInbox, list[EventBody]]:
    events: list[EventBody] = []
    return ConversationInbox(OPERATOR_ID, "c1", sink=events.append), events


async def test_idle_inboxes_are_dropped_but_never_one_holding_something():
    async with client_app() as (_client, app):
        runs = app_state(app).runs
        idle = runs.inbox("idle", OPERATOR_ID)
        busy = runs.inbox("busy", OPERATOR_ID)
        _ = busy.enqueue("keep me")
        stopped = runs.inbox("stopped", OPERATOR_ID)
        _ = stopped.stop()

        for n in range(400):
            _ = runs.inbox(f"c{n}", OPERATOR_ID)

        assert runs.inbox("busy", OPERATOR_ID) is busy
        assert runs.inbox("stopped", OPERATOR_ID) is stopped
        assert runs.inbox("idle", OPERATOR_ID) is not idle


def test_the_first_word_after_a_stop_leads_what_waited_behind_it():
    inbox, _ = _inbox()
    earlier = inbox.enqueue("queued before the stop")
    report = inbox.enqueue("found it", source="subagent")
    _ = inbox.stop()
    now = inbox.enqueue("do this instead", ahead=True)

    assert inbox.lead() == now
    assert [m.id for m in inbox.deliverable()] == [now.id, earlier.id, report.id]


def test_delivery_follows_the_hierarchy_not_arrival():
    inbox, _ = _inbox()
    notice = inbox.enqueue("open steps", source="system", notice="open-tasks")
    agent = inbox.enqueue("only the parser", source="agent", sender="lexer")
    report = inbox.enqueue("found it", source="subagent")
    first = inbox.enqueue("first")
    second = inbox.enqueue("second")

    assert [m.id for m in inbox.drain()] == [first.id, second.id, report.id, agent.id, notice.id]
    assert inbox.messages == ()


def test_a_hold_stalls_only_the_operators_own_messages():
    inbox, _ = _inbox()
    draft = inbox.enqueue("still writing")
    after = inbox.enqueue("and this")
    report = inbox.enqueue("found it", source="subagent")
    assert inbox.hold(draft.id, True)

    assert [m.id for m in inbox.drain()] == [report.id]
    assert inbox.hold(draft.id, False)
    assert [m.id for m in inbox.drain()] == [draft.id, after.id]


def test_a_turn_item_is_never_injected_and_leads_the_next_turn():
    inbox, _ = _inbox()
    text = inbox.enqueue("look at this")
    turn = inbox.enqueue("and this file", payload=TurnPayload(attachment_ids=("u1",)))
    behind = inbox.enqueue("then this")
    report = inbox.enqueue("found it", source="subagent")

    assert [m.id for m in inbox.drain()] == [text.id, report.id]
    assert inbox.lead() == turn
    inbox.take(turn)
    assert [m.id for m in inbox.drain()] == [behind.id]


def test_the_stop_holds_the_floor_and_says_so_once():
    inbox, events = _inbox()
    _ = inbox.stop()
    assert inbox.waiting_for_operator
    assert inbox.take_stop_notice() == STOP_NOTICE
    assert inbox.take_stop_notice() is None
    inbox.resume()
    assert not inbox.waiting_for_operator
    assert [e.waiting for e in events if isinstance(e, InboxWaiting)] == [True, False]


def test_every_message_is_delivered_once_or_withdrawn():
    """Random interleavings of everything that can happen to an inbox: nothing is lost and
    nothing is delivered twice."""
    for seed in range(200):
        rng = random.Random(seed)
        inbox, events = _inbox()
        queued: list[QueuedMessage] = []
        withdrawn: set[str] = set()
        for _ in range(40):
            action = rng.choice(["enqueue", "enqueue", "hold", "withdraw", "drain", "take"])
            waiting = list(inbox.messages)
            if action == "enqueue":
                source = rng.choice(_SOURCES)
                payload = TurnPayload(file_refs=("a.py",)) if rng.random() < 0.2 else None
                queued.append(
                    inbox.enqueue(
                        "x", source=source, payload=payload if source == "operator" else None
                    )
                )
            elif action == "hold" and waiting:
                _ = inbox.hold(rng.choice(waiting).id, rng.random() < 0.5)
            elif action == "withdraw" and waiting:
                chosen = rng.choice(waiting)
                assert inbox.withdraw(chosen.id)
                withdrawn.add(chosen.id)
            elif action == "drain":
                _ = inbox.drain()
            elif action == "take" and (lead := inbox.lead()) is not None:
                inbox.take(lead)
        for message in list(inbox.messages):
            _ = inbox.hold(message.id, False)
        while inbox.messages:
            lead = inbox.lead()
            assert lead is not None
            inbox.take(lead)
            _ = inbox.drain()
        delivered = [e.message_id for e in events if isinstance(e, MessageInjected)]
        assert len(delivered) == len(set(delivered)), seed
        assert set(delivered) | withdrawn == {m.id for m in queued}, seed
        assert not set(delivered) & withdrawn, seed


# -- persistence --------------------------------------------------------------------------


@asynccontextmanager
async def _journal(tmp_path: Path) -> AsyncGenerator[SealedInboxJournal]:
    """A journal over a database of its own. Inside a booted app, the app's restore would
    read these rows mid-test and dispatch them as a real inbox."""
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    vault = Vault(tmp_path / "keyfile.json")
    await vault.setup("pw")
    journal = SealedInboxJournal(engine, vault)
    await journal.start()
    try:
        yield journal
    finally:
        await journal.stop()


async def test_the_journal_brings_an_inbox_back_after_a_restart(tmp_path: Path):
    async with _journal(tmp_path) as journal:
        inbox = ConversationInbox(OPERATOR_ID, "c1", journal=journal)
        kept = inbox.enqueue("keep me", payload=TurnPayload(attachment_ids=("u1",)))
        report = inbox.enqueue("found it", source="subagent", sender="reader")
        gone = inbox.enqueue("never mind")
        _ = inbox.hold(kept.id, True)
        _ = inbox.withdraw(gone.id)
        _ = inbox.stop()
        await journal.flush()

        [loaded] = await journal.load()

    assert loaded.waiting
    assert [(m.id, m.text, m.held, m.payload) for m in loaded.messages] == [
        (kept.id, "keep me", True, TurnPayload(attachment_ids=("u1",))),
        (report.id, "found it", False, None),
    ]
    assert loaded.messages[1].sender == "reader"


async def test_the_stop_notice_survives_a_restart(tmp_path: Path):
    async with _journal(tmp_path) as journal:
        inbox = ConversationInbox(OPERATOR_ID, "c1", journal=journal)
        _ = inbox.stop()
        await journal.flush()
        [stopped] = await journal.load()

        assert inbox.take_stop_notice() == STOP_NOTICE
        await journal.flush()
        [told] = await journal.load()

    assert stopped.waiting and stopped.stopped
    assert told.waiting and not told.stopped

    fresh = ConversationInbox(OPERATOR_ID, "c1")
    fresh.restore([], waiting=stopped.waiting, stopped=stopped.stopped)
    assert fresh.take_stop_notice() == STOP_NOTICE


async def test_a_restored_message_is_journaled_again(tmp_path: Path):
    async with _journal(tmp_path) as journal:
        inbox = ConversationInbox(OPERATOR_ID, "c1", journal=journal)
        item = inbox.enqueue("keep me")
        inbox.take(item)
        await journal.flush()
        assert not await journal.load()

        inbox.restore([item], waiting=False)
        await journal.flush()
        [loaded] = await journal.load()

    assert [m.id for m in loaded.messages] == [item.id]


def test_restore_puts_a_taken_message_back_in_its_place_and_only_once():
    inbox, _ = _inbox()
    first = inbox.enqueue("first")
    second = inbox.enqueue("second")
    inbox.take(first)
    assert [m.id for m in inbox.messages] == [second.id]

    inbox.restore([first])
    inbox.restore([first, second])
    assert [m.id for m in inbox.messages] == [first.id, second.id]


def test_a_message_sent_before_the_restore_sorts_after_the_restored_ones():
    inbox, _ = _inbox()
    fresh = inbox.enqueue("after the boot")
    stored = QueuedMessage(id="old", text="from before", queued_at=fresh.queued_at, seq=1)
    ahead = QueuedMessage(id="far", text="from later", queued_at=fresh.queued_at, seq=fresh.seq + 5)

    inbox.restore([stored, ahead])
    after = inbox.enqueue("next")

    assert [m.id for m in inbox.messages] == ["old", fresh.id, "far", after.id]
    assert after.seq > ahead.seq


# -- through the app ----------------------------------------------------------------------


def _user_texts(request: ModelRequest) -> list[str]:
    texts: list[str] = []
    for part in request.parts:
        if not isinstance(part, UserPromptPart):
            continue
        content = part.content if isinstance(part.content, list) else [part.content]
        texts.extend(c for c in content if isinstance(c, str))
    return texts


def _recording_model(
    monkeypatch: pytest.MonkeyPatch, seen: list[list[str]], gate: asyncio.Event
) -> asyncio.Event:
    """A model that records the user texts of every request it gets, and holds its answer
    until ``gate`` is set. Returns an event set once the first request has arrived."""
    started = asyncio.Event()

    async def stream_fn(messages: list[ModelMessage], _info: AgentInfo) -> AsyncIterator[str]:
        last = messages[-1]
        assert isinstance(last, ModelRequest)
        seen.append(_user_texts(last))
        started.set()
        _ = await gate.wait()
        yield "ok"

    async def resolve_detailed(self: ModelRegistry, _role: str, **_kwargs: object):
        return await stub_resolution(self, FunctionModel(stream_function=stream_fn))

    register_stub_provider(monkeypatch)
    monkeypatch.setattr(ModelRegistry, "resolve_detailed", resolve_detailed)
    return started


async def test_a_busy_thread_queues_and_the_operator_manages_their_own(
    monkeypatch: pytest.MonkeyPatch,
):
    gate = asyncio.Event()
    started = _recording_model(monkeypatch, [], gate)
    async with client_app() as (client, app):
        first = json_body(await client.post("/chat", json={"prompt": "hello"}))
        conversation_id = s(first["conversation_id"])
        _ = await started.wait()

        queued = json_body(
            await client.post("/chat", json={"prompt": "also", "conversation_id": conversation_id})
        )
        assert queued["run_id"] == first["run_id"]
        message_id = s(queued["queued_message_id"])
        report = app_state(app).runs.inbox(conversation_id, OPERATOR_ID).enqueue(
            "found it", source="subagent"
        )

        listed = json_body(await client.get(f"/conversations/{conversation_id}/inbox"))
        assert [obj(m)["source"] for m in arr(listed["messages"])] == ["operator", "subagent"]

        base = f"/conversations/{conversation_id}/inbox"
        edited = await client.patch(f"{base}/{message_id}", json={"text": "also this"})
        assert json_body(edited)["text"] == "also this"
        held = await client.post(f"{base}/{message_id}/hold", json={"held": True})
        assert json_body(held)["held"] is True
        # A report is its sender's words, not the operator's to change.
        assert (await client.patch(f"{base}/{report.id}", json={"text": "x"})).status_code == 403
        assert (await client.delete(f"{base}/{report.id}")).status_code == 403
        assert (await client.delete(f"{base}/{message_id}")).status_code == 200
        assert (await client.delete(f"{base}/{message_id}")).status_code == 404

        gate.set()
        await settle(app)


async def test_after_a_stop_everything_waits_behind_the_operators_next_message(
    monkeypatch: pytest.MonkeyPatch,
):
    seen: list[list[str]] = []
    gate = asyncio.Event()
    started = _recording_model(monkeypatch, seen, gate)
    async with client_app() as (client, app):
        first = json_body(await client.post("/chat", json={"prompt": "hello"}))
        conversation_id = s(first["conversation_id"])
        _ = await started.wait()
        body = {"prompt": "earlier", "conversation_id": conversation_id}
        assert json_body(await client.post("/chat", json=body))["queued_message_id"]

        stopped = await client.post(f"/runs/{s(first['run_id'])}/cancel", json={"hold_inbox": True})
        assert stopped.status_code == 202
        await settle(app)
        inbox = app_state(app).runs.inbox(conversation_id, OPERATOR_ID)
        assert inbox.waiting_for_operator

        # A report arriving now waits too: nothing starts behind a deliberate Stop.
        wake = cast(object, app.state.subagent_wake)
        assert isinstance(wake, SubagentWake)
        _ = await wake.deliver(OPERATOR_ID, conversation_id, "found it")
        await settle(app)
        assert app_state(app).runs.active_run_for(conversation_id, OPERATOR_ID) is None
        assert len(inbox.messages) == 2

        gate.set()
        seen.clear()
        resumed = json_body(
            await client.post("/chat", json={"prompt": "now", "conversation_id": conversation_id})
        )
        assert "queued_message_id" not in resumed
        await settle(app)

        # The stop notice, then the new message, then the operator's earlier one, then the
        # report — all in the first request of the turn. The stopped turn's own prompt,
        # never answered, merges in ahead of them.
        texts = seen[0]
        at = texts.index(STOP_NOTICE)
        assert texts[at + 1 : at + 3] == ["now", "earlier"]
        assert "found it" in texts[at + 3]
        assert inbox.messages == () and not inbox.waiting_for_operator

        # The notice framed the turn; it is not the operator's words on record.
        detail = json_body(await client.get(f"/conversations/{conversation_id}"))
        users = [s(obj(m)["content"]) for m in arr(detail["messages"]) if obj(m)["role"] == "user"]
        assert all(STOP_NOTICE not in text for text in users)


async def test_a_failed_run_hands_its_inbox_to_the_next_turn(monkeypatch: pytest.MonkeyPatch):
    seen: list[list[str]] = []
    gate = asyncio.Event()
    started = _recording_model(monkeypatch, seen, gate)
    async with client_app() as (client, app):
        first = json_body(await client.post("/chat", json={"prompt": "hello"}))
        conversation_id = s(first["conversation_id"])
        _ = await started.wait()
        _ = await client.post("/chat", json={"prompt": "next", "conversation_id": conversation_id})

        # A plain cancel, not the operator's Stop: the inbox keeps flowing.
        run = app_state(app).runs.get(s(first["run_id"]))
        assert run is not None
        gate.set()
        _ = await app_state(app).runs.cancel(run.id)
        await settle(app)

        assert run.status is RunStatus.cancelled
        assert any("next" in request for request in seen)
        assert app_state(app).runs.inbox(conversation_id, OPERATOR_ID).messages == ()


async def test_resume_releases_the_inbox_without_a_message(monkeypatch: pytest.MonkeyPatch):
    seen: list[list[str]] = []
    gate = asyncio.Event()
    started = _recording_model(monkeypatch, seen, gate)
    async with client_app() as (client, app):
        first = json_body(await client.post("/chat", json={"prompt": "hello"}))
        conversation_id = s(first["conversation_id"])
        _ = await started.wait()
        body = {"prompt": "queued", "conversation_id": conversation_id}
        _ = await client.post("/chat", json=body)
        _ = await client.post(f"/runs/{s(first['run_id'])}/cancel", json={"hold_inbox": True})
        await settle(app)
        gate.set()
        seen.clear()

        resumed = json_body(await client.post(f"/conversations/{conversation_id}/inbox/resume"))
        await settle(app)

        assert resumed["waiting_for_operator"] is False
        texts = seen[0]
        assert texts[texts.index(STOP_NOTICE) + 1] == "queued"


async def test_a_stop_after_the_run_ended_holds_nothing(monkeypatch: pytest.MonkeyPatch):
    seen: list[list[str]] = []
    gate = asyncio.Event()
    gate.set()
    started = _recording_model(monkeypatch, seen, gate)
    async with client_app() as (client, app):
        first = json_body(await client.post("/chat", json={"prompt": "hello"}))
        conversation_id = s(first["conversation_id"])
        _ = await started.wait()
        await settle(app)

        late = await client.post(f"/runs/{s(first['run_id'])}/cancel", json={"hold_inbox": True})
        assert late.status_code == 409
        inbox = app_state(app).runs.inbox(conversation_id, OPERATOR_ID)
        assert not inbox.waiting_for_operator
        assert inbox.take_stop_notice() is None

        wake = cast(object, app.state.subagent_wake)
        assert isinstance(wake, SubagentWake)
        _ = await wake.deliver(OPERATOR_ID, conversation_id, "found it")
        await settle(app)
        assert inbox.messages == ()
        assert any("found it" in text for request in seen for text in request)


async def test_withdrawing_a_held_message_releases_those_behind_it(
    monkeypatch: pytest.MonkeyPatch,
):
    seen: list[list[str]] = []
    gate = asyncio.Event()
    started = _recording_model(monkeypatch, seen, gate)
    async with client_app() as (client, app):
        first = json_body(await client.post("/chat", json={"prompt": "hello"}))
        conversation_id = s(first["conversation_id"])
        _ = await started.wait()
        body = {"conversation_id": conversation_id}
        a = json_body(await client.post("/chat", json={"prompt": "draft", **body}))
        _ = await client.post("/chat", json={"prompt": "behind", **body})
        base = f"/conversations/{conversation_id}/inbox"
        _ = await client.post(f"{base}/{s(a['queued_message_id'])}/hold", json={"held": True})

        gate.set()
        await settle(app)
        inbox = app_state(app).runs.inbox(conversation_id, OPERATOR_ID)
        assert len(inbox.messages) == 2
        seen.clear()

        assert (await client.delete(f"{base}/{s(a['queued_message_id'])}")).status_code == 200
        await settle(app)

        assert inbox.messages == ()
        assert any("behind" in text for request in seen for text in request)


async def test_deleting_a_conversation_clears_its_inbox_and_rows(monkeypatch: pytest.MonkeyPatch):
    gate = asyncio.Event()
    started = _recording_model(monkeypatch, [], gate)
    async with client_app() as (client, app):
        state = app_state(app)
        journal = SealedInboxJournal(state.db_engine, state.vault)
        await journal.start()
        state.runs.attach_journal(journal)
        try:
            first = json_body(await client.post("/chat", json={"prompt": "hello"}))
            conversation_id = s(first["conversation_id"])
            _ = await started.wait()
            body = {"prompt": "queued", "conversation_id": conversation_id}
            _ = await client.post("/chat", json=body)
            _ = await client.post(f"/runs/{s(first['run_id'])}/cancel", json={"hold_inbox": True})
            await settle(app)
            await journal.flush()
            assert [e for e in await journal.load() if e.conversation_id == conversation_id]

            assert (await client.delete(f"/conversations/{conversation_id}")).status_code == 204
            await journal.flush()

            assert not [e for e in await journal.load() if e.conversation_id == conversation_id]
            inbox = state.runs.inbox(conversation_id, OPERATOR_ID)
            assert inbox.messages == () and not inbox.waiting_for_operator
        finally:
            gate.set()
            await journal.stop()
