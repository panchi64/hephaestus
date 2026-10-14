"""The notifications HTTP surface: REST list/read/read_all, the SSE stream, auth
gating, and the camelCase out-shape."""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from itertools import count as _count

from routes.deps import OPERATOR_ID

from ._helpers import client_app


def _ticking_clock(monkeypatch):
    """A deterministic, strictly-increasing clock so ordering/pagination assertions
    never race real wall-clock resolution."""
    ticks = _count()
    base = datetime(2026, 1, 1, tzinfo=UTC)

    def fake_utcnow() -> datetime:
        return base + timedelta(seconds=next(ticks))

    monkeypatch.setattr("services.notifications.utcnow", fake_utcnow)


async def _collect_stream_frames(service, *, after_seq: int, n: int):
    """Drive the notification stream's frame generator directly, bounded to `n` frames.

    The stream is deliberately process-lifetime (it never ends on its own), and
    httpx's `ASGITransport` fully buffers a request before returning anything —
    it can't drive a response whose body never finishes, so a real end-to-end
    `client.stream(...)` against this endpoint would just hang. Driving the
    response's own `body_iterator` sidesteps that transport limitation entirely
    while still exercising the exact code the route serves over HTTP.
    """
    from core.sse import sse_stream
    from routes.notifications import _frame

    resp = sse_stream(lambda: service.subscribe(after_seq), _frame)
    frames = []
    gen = resp.body_iterator
    assert isinstance(gen, AsyncGenerator)
    try:
        async for chunk in gen:
            assert isinstance(chunk, str)
            for line in chunk.splitlines():
                if line.startswith("data:"):
                    frames.append(json.loads(line[len("data:") :].strip()))
            if len(frames) >= n:
                break
    finally:
        await gen.aclose()
    return frames


# --- REST list: pagination / unread filter / unreadCount -----------------------


async def test_list_pagination_unread_filter_and_unread_count(monkeypatch):
    _ticking_clock(monkeypatch)
    async with client_app() as (client, app):
        service = app.state.notifications
        views = [await service.notify(OPERATOR_ID, "system", f"n{i}") for i in range(3)]
        await service.mark_read(OPERATOR_ID, [views[0].id])

        body = (await client.get("/notifications")).json()
        assert body["unreadCount"] == 2
        assert [item["title"] for item in body["items"]] == ["n2", "n1", "n0"]  # newest first

        page = (await client.get("/notifications", params={"limit": 1})).json()
        assert [item["title"] for item in page["items"]] == ["n2"]

        unread_only = (await client.get("/notifications", params={"unread_only": True})).json()
        assert {item["title"] for item in unread_only["items"]} == {"n1", "n2"}

        before = (
            await client.get("/notifications", params={"before": views[2].created_at.isoformat()})
        ).json()
        assert {item["title"] for item in before["items"]} == {"n0", "n1"}


async def test_list_spans_rehydrated_and_live_notifications(monkeypatch):
    """After a restart the cache holds both DB-read notifications (SQLite has no tz type,
    so they come back naive) and ones `notify()` cached since — the route sorts and
    filters across both, and must not 500 on a naive/aware comparison."""
    _ticking_clock(monkeypatch)
    async with client_app() as (client, app):
        service = app.state.notifications
        old = await service.notify(OPERATOR_ID, "system", "persisted")
        await service._worker.join()
        # Drop it from the cache and reload from the DB — the restart path, without a
        # second app: `_rehydrate` skips ids already cached.
        service._cache.pop(old.id)
        await service._rehydrate()
        fresh = await service.notify(OPERATOR_ID, "system", "live")

        resp = await client.get("/notifications")
        assert resp.status_code == 200
        assert [item["title"] for item in resp.json()["items"]] == ["live", "persisted"]

        # `before` is aware only when the client sends an offset — both forms filter.
        for cutoff in (fresh.created_at, fresh.created_at.replace(tzinfo=None)):
            page = await client.get("/notifications", params={"before": cutoff.isoformat()})
            assert page.status_code == 200
            assert [item["id"] for item in page.json()["items"]] == [old.id]


async def test_notification_out_is_camel_case():
    async with client_app() as (client, app):
        await app.state.notifications.notify(
            OPERATOR_ID,
            "approval_needed",
            "t",
            "b",
            conversation_id="conv-1",
            run_id="run-1",
        )
        body = (await client.get("/notifications")).json()
        item = body["items"][0]
        assert set(item) == {
            "id",
            "kind",
            "title",
            "body",
            "conversationId",
            "runId",
            "createdAt",
            "readAt",
            "resolvedAt",
        }
        assert item["conversationId"] == "conv-1"
        assert item["runId"] == "run-1"
        assert item["readAt"] is None
        assert item["resolvedAt"] is None


# --- read / read_all -----------------------------------------------------------


async def test_mark_read_route_updates_and_reports_count():
    async with client_app() as (client, app):
        view = await app.state.notifications.notify(OPERATOR_ID, "system", "a")

        resp = await client.post("/notifications/read", json={"ids": [view.id]})
        assert resp.json() == {"updated": 1}

        again = await client.post("/notifications/read", json={"ids": [view.id]})
        assert again.json() == {"updated": 0}


async def test_mark_all_read_route():
    async with client_app() as (client, app):
        service = app.state.notifications
        await service.notify(OPERATOR_ID, "system", "a")
        await service.notify(OPERATOR_ID, "system", "b")

        resp = await client.post("/notifications/read_all")
        assert resp.json() == {"updated": 2}

        listing = (await client.get("/notifications")).json()
        assert listing["unreadCount"] == 0


# --- SSE stream ------------------------------------------------------------------


async def test_stream_delivers_created_then_replays_after_reconnect():
    async with client_app() as (client, app):
        service = app.state.notifications
        await service.notify(OPERATOR_ID, "system", "a")
        await service.notify(OPERATOR_ID, "system", "b")

        frames = await _collect_stream_frames(service, after_seq=0, n=2)
        assert [f["notification"]["title"] for f in frames] == ["a", "b"]
        assert frames[0]["type"] == "notification.created"
        last_seq = frames[-1]["seq"]

        await service.notify(OPERATOR_ID, "system", "c")
        resumed = await _collect_stream_frames(service, after_seq=last_seq, n=1)
        assert resumed[0]["notification"]["title"] == "c"
        assert resumed[0]["seq"] > last_seq


# --- auth --------------------------------------------------------------------


async def test_notifications_routes_require_auth():
    async with client_app(auth_enabled=True, passphrase=None) as (client, _app):
        assert (await client.get("/notifications")).status_code == 401
        assert (await client.post("/notifications/read", json={"ids": []})).status_code == 401
        assert (await client.post("/notifications/read_all")).status_code == 401
        # A 401 is decided by the auth gate before the route ever streams anything,
        # so a plain request (no need to open it as a stream) already proves the gate.
        assert (await client.get("/notifications/stream")).status_code == 401
