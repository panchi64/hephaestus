"""Substrate HTTP surface: SSE streaming, resume, cancel, and 404s."""

from __future__ import annotations

import asyncio

import pytest

from routes.deps import app_state
from runs import AnswerDelta, Run

from ._helpers import client_app, collect_sse_events
from ._json import json_body, json_list, n, obj, s


async def test_stream_delivers_full_event_record():
    async with client_app() as (client, app):

        async def orch(run: Run) -> None:
            _ = run.emit(AnswerDelta(text="hello"))

        run = app_state(app).runs.submit(kind="chat", owner_id="operator", orchestrator=orch)
        events = await collect_sse_events(client, run.id)

    types = [e["type"] for e in events]
    assert types[0] == "run.started"
    assert "answer.delta" in types
    assert types[-1] == "run.ended"
    assert [n(e["seq"]) for e in events] == sorted(n(e["seq"]) for e in events)


async def test_resume_replays_only_missed_events():
    async with client_app() as (client, app):

        async def orch(run: Run) -> None:
            _ = run.emit(AnswerDelta(text="a"))
            _ = run.emit(AnswerDelta(text="b"))

        run = app_state(app).runs.submit(kind="chat", owner_id="operator", orchestrator=orch)
        await run.wait()
        events = await collect_sse_events(client, run.id, last_event_id=2)

    assert all(n(e["seq"]) > 2 for e in events)
    assert events[-1]["type"] == "run.ended"


async def test_cancel_endpoint():
    async with client_app() as (client, app):

        async def orch(run: Run) -> None:
            _ = run.emit(AnswerDelta(text="working"))
            _ = await asyncio.Event().wait()

        run = app_state(app).runs.submit(kind="agent", owner_id="operator", orchestrator=orch)
        await asyncio.sleep(0)

        resp = await client.post(f"/runs/{run.id}/cancel")
        assert resp.status_code == 202
        await run.wait()

        status = await client.get(f"/runs/{run.id}")
        assert json_body(status)["status"] == "cancelled"


async def test_cancel_terminal_run_conflicts():
    async with client_app() as (client, app):

        async def orch(_run: Run) -> None:
            return None

        run = app_state(app).runs.submit(kind="chat", owner_id="operator", orchestrator=orch)
        await run.wait()
        resp = await client.post(f"/runs/{run.id}/cancel")
        assert resp.status_code == 409


async def test_list_runs_active_only_by_default():
    async with client_app() as (client, app):
        runs = app_state(app).runs

        async def blocked(run: Run) -> None:
            _ = run.emit(AnswerDelta(text="working"))
            _ = await asyncio.Event().wait()

        async def done(_run: Run) -> None:
            return None

        live = runs.submit(kind="chat", owner_id="operator", orchestrator=blocked)
        finished = runs.submit(kind="chat", owner_id="operator", orchestrator=done)
        await finished.wait()
        await asyncio.sleep(0)

        active = json_list(await client.get("/runs"))
        assert {s(obj(r)["id"]) for r in active} == {live.id}

        everything = json_list(await client.get("/runs", params={"active": False}))
        assert {finished.id, live.id} <= {s(obj(r)["id"]) for r in everything}

        _ = await runs.cancel(live.id)
        await live.wait()


async def test_list_runs_empty():
    async with client_app() as (client, _app):
        assert json_list(await client.get("/runs")) == []


async def test_list_runs_enriches_with_conversation_id_and_title():
    async with client_app() as (client, app):
        conversations = app_state(app).conversations
        conv_id = await conversations.create_conversation("operator")
        await conversations.set_title(conv_id, "Weekend plans")
        started, release = asyncio.Event(), asyncio.Event()

        async def orch(_run: Run) -> None:
            started.set()
            _ = await release.wait()

        run = app_state(app).runs.submit(
            kind="chat", owner_id="operator", orchestrator=orch, conversation_id=conv_id
        )
        _ = await started.wait()

        body = json_list(await client.get("/runs"))
        assert len(body) == 1
        first = obj(body[0])
        assert first["conversationId"] == conv_id
        assert first["conversationTitle"] == "Weekend plans"
        assert first["status"] in ("queued", "running", "awaiting_input")
        # Existing fields keep their original (snake_case) key — backward compatible.
        assert first["owner_id"] == "operator"

        release.set()
        await run.wait()


async def test_listing_many_runs_reads_titles_once_and_still_matches_them_up(
    monkeypatch: pytest.MonkeyPatch,
):
    # The listing used to ask for a full conversation summary per run — every thread's rows
    # read to produce one title. It now resolves the whole page in one query; each run must
    # still get *its own* title, and an untitled thread must stay null rather than
    # borrowing a neighbour's.
    async with client_app() as (client, app):
        store = app_state(app).conversations
        titled: list[tuple[str, str]] = []
        for name in ("Weekend plans", "Tax return", "Bike repair"):
            conv_id = await store.create_conversation("operator")
            await store.set_title(conv_id, name)
            titled.append((conv_id, name))
        untitled = await store.create_conversation("operator")

        release = asyncio.Event()
        started: list[asyncio.Event] = []

        async def orch(_run: Run) -> None:
            started[-1].set()
            _ = await release.wait()

        runs: list[Run] = []
        for conv_id in [c for c, _ in titled] + [untitled]:
            started.append(asyncio.Event())
            runs.append(
                app_state(app).runs.submit(
                    kind="chat", owner_id="operator", orchestrator=orch, conversation_id=conv_id
                )
            )
            _ = await started[-1].wait()

        calls: list[list[str]] = []
        original = store.titles

        async def counted(ids: list[str], owner_id: str) -> dict[str, str]:
            calls.append(list(ids))
            return await original(ids, owner_id)

        monkeypatch.setattr(store, "titles", counted)
        body = json_list(await client.get("/runs"))

        by_conversation = {
            s(obj(row)["conversationId"]): obj(row)["conversationTitle"] for row in body
        }
        assert by_conversation == {**dict(titled), untitled: None}
        assert len(calls) == 1  # one read for four runs, not four

        release.set()
        for run in runs:
            await run.wait()


async def test_get_run_conversation_fields_are_null_without_a_conversation():
    async with client_app() as (client, app):

        async def orch(_run: Run) -> None:
            return None

        run = app_state(app).runs.submit(kind="chat", owner_id="operator", orchestrator=orch)
        await run.wait()

        body = json_body(await client.get(f"/runs/{run.id}"))
        assert body["conversationId"] is None
        assert body["conversationTitle"] is None


async def test_unknown_run_is_404():
    async with client_app() as (client, _app):
        assert (await client.get("/runs/nope")).status_code == 404
        assert (await client.post("/runs/nope/cancel")).status_code == 404
        async with client.stream("GET", "/runs/nope/events") as resp:
            assert resp.status_code == 404
