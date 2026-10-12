"""Substrate HTTP surface: SSE streaming, resume, cancel, and 404s."""

from __future__ import annotations

import asyncio

from runs.events import AnswerDelta

from ._helpers import client_app, collect_sse_events


async def test_stream_delivers_full_event_record():
    async with client_app() as (client, app):

        async def orch(run):
            run.emit(AnswerDelta(text="hello"))

        run = app.state.runs.submit(kind="chat", owner_id="operator", orchestrator=orch)
        events = await collect_sse_events(client, run.id)

    types = [e["type"] for e in events]
    assert types[0] == "run.started"
    assert "answer.delta" in types
    assert types[-1] == "run.ended"
    assert [e["seq"] for e in events] == sorted(e["seq"] for e in events)


async def test_resume_replays_only_missed_events():
    async with client_app() as (client, app):

        async def orch(run):
            run.emit(AnswerDelta(text="a"))
            run.emit(AnswerDelta(text="b"))

        run = app.state.runs.submit(kind="chat", owner_id="operator", orchestrator=orch)
        await run.wait()
        events = await collect_sse_events(client, run.id, last_event_id=2)

    assert all(e["seq"] > 2 for e in events)
    assert events[-1]["type"] == "run.ended"


async def test_cancel_endpoint():
    async with client_app() as (client, app):

        async def orch(run):
            run.emit(AnswerDelta(text="working"))
            await asyncio.Event().wait()

        run = app.state.runs.submit(kind="agent", owner_id="operator", orchestrator=orch)
        await asyncio.sleep(0)

        resp = await client.post(f"/runs/{run.id}/cancel")
        assert resp.status_code == 202
        await run.wait()

        status = await client.get(f"/runs/{run.id}")
        assert status.json()["status"] == "cancelled"


async def test_cancel_terminal_run_conflicts():
    async with client_app() as (client, app):

        async def orch(run):
            return None

        run = app.state.runs.submit(kind="chat", owner_id="operator", orchestrator=orch)
        await run.wait()
        resp = await client.post(f"/runs/{run.id}/cancel")
        assert resp.status_code == 409


async def test_withdraw_queued_message_matrix():
    async with client_app() as (client, app):
        gate = asyncio.Event()

        async def orch(run):
            await gate.wait()

        run = app.state.runs.submit(kind="chat", owner_id="operator", orchestrator=orch)
        await asyncio.sleep(0)
        message = run.enqueue_message("wait, also")

        # Withdraw while live and pending → 200, inbox emptied.
        resp = await client.delete(f"/runs/{run.id}/messages/{message.id}")
        assert resp.status_code == 200
        assert run.pending_messages == []

        # Same id again (already withdrawn) and an unknown id → 404.
        assert (await client.delete(f"/runs/{run.id}/messages/{message.id}")).status_code == 404
        assert (await client.delete(f"/runs/{run.id}/messages/nope")).status_code == 404
        # Unknown run → 404.
        assert (await client.delete("/runs/nope/messages/x")).status_code == 404

        # A message still queued when the run ends can't be withdrawn either.
        stranded = run.enqueue_message("too late")
        gate.set()
        await run.wait()
        assert (await client.delete(f"/runs/{run.id}/messages/{stranded.id}")).status_code == 404

        types = [e.body.type for e in run.stream.replay()]
        assert types.count("message.queued") == 2
        assert types.count("message.withdrawn") == 1
        assert types.count("message.injected") == 0


async def test_edit_queued_message_matrix():
    async with client_app() as (client, app):
        gate = asyncio.Event()

        async def orch(run):
            await gate.wait()

        run = app.state.runs.submit(kind="chat", owner_id="operator", orchestrator=orch)
        await asyncio.sleep(0)
        message = run.enqueue_message("first draft")

        # Edit while live and pending → 200, text rewritten in place (same id).
        resp = await client.patch(
            f"/runs/{run.id}/messages/{message.id}", json={"text": "final wording"}
        )
        assert resp.status_code == 200
        assert [(m.id, m.text) for m in run.pending_messages] == [(message.id, "final wording")]

        # Blank text is rejected without touching the queue.
        resp = await client.patch(f"/runs/{run.id}/messages/{message.id}", json={"text": "   "})
        assert resp.status_code == 422
        assert run.pending_messages[0].text == "final wording"

        # A withdrawn message, an unknown id, and an unknown run → 404.
        assert run.withdraw_message(message.id)
        assert (
            await client.patch(f"/runs/{run.id}/messages/{message.id}", json={"text": "x"})
        ).status_code == 404
        assert (
            await client.patch(f"/runs/{run.id}/messages/nope", json={"text": "x"})
        ).status_code == 404
        assert (await client.patch("/runs/nope/messages/x", json={"text": "x"})).status_code == 404

        # A message still queued when the run ends can't be edited either.
        stranded = run.enqueue_message("too late")
        gate.set()
        await run.wait()
        assert (
            await client.patch(f"/runs/{run.id}/messages/{stranded.id}", json={"text": "x"})
        ).status_code == 404

        types = [e.body.type for e in run.stream.replay()]
        assert types.count("message.edited") == 1


async def test_hold_queued_message_matrix():
    async with client_app() as (client, app):
        gate = asyncio.Event()

        async def orch(run):
            await gate.wait()

        run = app.state.runs.submit(kind="chat", owner_id="operator", orchestrator=orch)
        await asyncio.sleep(0)
        message = run.enqueue_message("still writing this")

        # Hold while live and pending → 200, the message stays queued and held.
        resp = await client.post(f"/runs/{run.id}/messages/{message.id}/hold", json={"held": True})
        assert resp.status_code == 200
        assert [(m.id, m.held) for m in run.pending_messages] == [(message.id, True)]
        assert run.drain_messages() == []

        # Holding again is the state it is already in — a 200, and no second frame.
        assert (
            await client.post(f"/runs/{run.id}/messages/{message.id}/hold", json={"held": True})
        ).status_code == 200

        # Released, and the drain takes it.
        assert (
            await client.post(f"/runs/{run.id}/messages/{message.id}/hold", json={"held": False})
        ).status_code == 200
        assert [m.id for m in run.drain_messages()] == [message.id]

        # An injected message, an unknown id, and an unknown run → 404.
        assert (
            await client.post(f"/runs/{run.id}/messages/{message.id}/hold", json={"held": True})
        ).status_code == 404
        assert (
            await client.post(f"/runs/{run.id}/messages/nope/hold", json={"held": True})
        ).status_code == 404
        assert (
            await client.post("/runs/nope/messages/x/hold", json={"held": True})
        ).status_code == 404

        # A message still queued when the run ends can't be held either: a terminal run
        # will never drain again, so there is nothing left to hold it back from.
        stranded = run.enqueue_message("too late")
        gate.set()
        await run.wait()
        assert (
            await client.post(f"/runs/{run.id}/messages/{stranded.id}/hold", json={"held": True})
        ).status_code == 404

        types = [e.body.type for e in run.stream.replay()]
        assert types.count("message.held") == 2  # on, then off — the no-op emits nothing


async def test_list_runs_active_only_by_default():
    async with client_app() as (client, app):

        async def blocked(run):
            run.emit(AnswerDelta(text="working"))
            await asyncio.Event().wait()

        async def done(run):
            return None

        live = app.state.runs.submit(kind="chat", owner_id="operator", orchestrator=blocked)
        finished = app.state.runs.submit(kind="chat", owner_id="operator", orchestrator=done)
        await finished.wait()
        await asyncio.sleep(0)

        active = (await client.get("/runs")).json()
        assert {r["id"] for r in active} == {live.id}

        everything = (await client.get("/runs", params={"active": False})).json()
        assert {finished.id, live.id} <= {r["id"] for r in everything}

        await app.state.runs.cancel(live.id)
        await live.wait()


async def test_list_runs_empty():
    async with client_app() as (client, _app):
        assert (await client.get("/runs")).json() == []


async def test_list_runs_enriches_with_conversation_id_and_title():
    async with client_app() as (client, app):
        conv_id = await app.state.conversations.create_conversation("operator")
        await app.state.conversations.set_title(conv_id, "Weekend plans")
        started, release = asyncio.Event(), asyncio.Event()

        async def orch(run):
            started.set()
            await release.wait()

        run = app.state.runs.submit(
            kind="chat", owner_id="operator", orchestrator=orch, conversation_id=conv_id
        )
        await started.wait()

        body = (await client.get("/runs")).json()
        assert len(body) == 1
        assert body[0]["conversationId"] == conv_id
        assert body[0]["conversationTitle"] == "Weekend plans"
        assert body[0]["status"] in ("queued", "running", "awaiting_input")
        # Existing fields keep their original (snake_case) key — backward compatible.
        assert body[0]["owner_id"] == "operator"

        release.set()
        await run.wait()


async def test_listing_many_runs_reads_titles_once_and_still_matches_them_up():
    # The listing used to ask for a full conversation summary per run — every thread's rows
    # read to produce one title. It now resolves the whole page in one query; each run must
    # still get *its own* title, and an untitled thread must stay null rather than
    # borrowing a neighbour's.
    async with client_app() as (client, app):
        store = app.state.conversations
        titled = []
        for name in ("Weekend plans", "Tax return", "Bike repair"):
            conv_id = await store.create_conversation("operator")
            await store.set_title(conv_id, name)
            titled.append((conv_id, name))
        untitled = await store.create_conversation("operator")

        release = asyncio.Event()
        started: list[asyncio.Event] = []

        async def orch(run):
            started[-1].set()
            await release.wait()

        runs = []
        for conv_id in [c for c, _ in titled] + [untitled]:
            started.append(asyncio.Event())
            runs.append(
                app.state.runs.submit(
                    kind="chat", owner_id="operator", orchestrator=orch, conversation_id=conv_id
                )
            )
            await started[-1].wait()

        calls: list[list[str]] = []
        original = store.titles

        async def counted(ids, owner_id):
            calls.append(list(ids))
            return await original(ids, owner_id)

        store.titles = counted
        body = (await client.get("/runs")).json()

        by_conversation = {row["conversationId"]: row["conversationTitle"] for row in body}
        assert by_conversation == {**dict(titled), untitled: None}
        assert len(calls) == 1  # one read for four runs, not four

        release.set()
        for run in runs:
            await run.wait()


async def test_get_run_conversation_fields_are_null_without_a_conversation():
    async with client_app() as (client, app):

        async def orch(run):
            return None

        run = app.state.runs.submit(kind="chat", owner_id="operator", orchestrator=orch)
        await run.wait()

        body = (await client.get(f"/runs/{run.id}")).json()
        assert body["conversationId"] is None
        assert body["conversationTitle"] is None


async def test_unknown_run_is_404():
    async with client_app() as (client, _app):
        assert (await client.get("/runs/nope")).status_code == 404
        assert (await client.post("/runs/nope/cancel")).status_code == 404
        async with client.stream("GET", "/runs/nope/events") as resp:
            assert resp.status_code == 404
