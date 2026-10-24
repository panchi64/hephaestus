"""`POST /data/clear/{kind}`: bulk clears of chats, memories, notifications and uploads."""

from __future__ import annotations

from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from routes.deps import OPERATOR_ID, app_state

from ._helpers import client_app, collect_sse_events, patch_model_resolution, settle
from ._json import arr, json_body, json_list, n, obj, s


async def _chat(
    client: httpx.AsyncClient,
    prompt: str,
    attachment_ids: list[str],
    conversation_id: str | None = None,
) -> str:
    payload: dict[str, object] = {"prompt": prompt, "attachment_ids": attachment_ids}
    if conversation_id is not None:
        payload["conversation_id"] = conversation_id
    body = json_body(await client.post("/chat", json=payload))
    _ = await collect_sse_events(client, s(body["run_id"]))
    return s(body["conversation_id"])


async def _upload(client: httpx.AsyncClient, name: str, mime: str) -> str:
    resp = await client.post("/uploads", files={"file": (name, name.encode(), mime)})
    return s(json_body(resp)["id"])


async def _spawn_subagent_thread(app: FastAPI, parent: str) -> str:
    """A hidden sub-agent thread under ``parent``, registered the way a launch records it."""
    state = app_state(app)
    child = await state.conversations.create_conversation(OPERATOR_ID, parent_id=parent)
    records = state.subagent_records
    assert records is not None
    await records.record(
        subagent_id=f"child-{uuid4().hex}",
        owner_id=OPERATOR_ID,
        parent_conversation_id=parent,
        root_conversation_id=parent,
        depth=1,
        child_conversation_id=child,
        run_id=f"run-{uuid4().hex}",
        parent_run_id=None,
        spec_name="explorer",
        handle=f"h-{uuid4().hex[:8]}",
        task="look",
        workspace_policy="shared",
        workspace_key=parent,
        delegation_id=None,
    )
    return child


async def _exists(client: httpx.AsyncClient, conversation_id: str) -> bool:
    return (await client.get(f"/conversations/{conversation_id}")).status_code == 200


async def test_clearing_chats_deletes_them_with_their_images_and_skips_busy_ones(
    monkeypatch: pytest.MonkeyPatch,
):
    patch_model_resolution(monkeypatch)
    async with client_app() as (client, app):
        image = await _upload(client, "a.png", "image/png")
        first = await _chat(client, "one", [image])
        busy = await _chat(client, "two", [])
        app_state(app).runs.claim(busy, OPERATOR_ID)

        result = json_body(await client.post("/data/clear/chats"))

        assert n(result["deleted"]) == 1
        assert [s(i) for i in arr(result["skipped_ids"])] == [busy]
        assert not await _exists(client, first)
        assert await _exists(client, busy)
        assert (await client.get(f"/uploads/{image}")).status_code == 404


async def test_clearing_chats_keeps_the_subagent_threads_of_a_busy_thread(
    monkeypatch: pytest.MonkeyPatch,
):
    patch_model_resolution(monkeypatch)
    async with client_app() as (client, app):
        busy = await _chat(client, "working", [])
        child = await _spawn_subagent_thread(app, busy)
        app_state(app).runs.claim(busy, OPERATOR_ID)

        _ = await client.post("/data/clear/chats")

        assert await _exists(client, child)


async def test_deleting_a_chat_purges_images_shared_only_with_its_subagents(
    monkeypatch: pytest.MonkeyPatch,
):
    patch_model_resolution(monkeypatch)
    async with client_app() as (client, app):
        image = await _upload(client, "a.png", "image/png")
        parent = await _chat(client, "look", [image])
        child = await _spawn_subagent_thread(app, parent)
        _ = await _chat(client, "look too", [image], conversation_id=child)
        await settle(app)

        assert (await client.delete(f"/conversations/{parent}")).status_code == 204

        assert not await _exists(client, child)
        assert (await client.get(f"/uploads/{image}")).status_code == 404


async def test_deleting_a_chat_deletes_its_notifications():
    async with client_app() as (client, app):
        state = app_state(app)
        conversation = await state.conversations.create_conversation(OPERATOR_ID, title="t")
        _ = await state.notifications.notify(
            OPERATOR_ID, "run_failed", "failed", conversation_id=conversation
        )
        _ = await state.notifications.notify(OPERATOR_ID, "system", "unrelated")

        assert (await client.delete(f"/conversations/{conversation}")).status_code == 204

        page = json_body(await client.get("/notifications"))
        assert [s(obj(i)["title"]) for i in arr(page["items"])] == ["unrelated"]


async def test_clearing_memories_deletes_every_memory():
    async with client_app() as (client, _app):
        for content in ("likes tea", "lives in Lisbon"):
            _ = await client.post("/memory", json={"content": content})

        result = json_body(await client.post("/data/clear/memories"))

        assert n(result["deleted"]) == 2
        assert json_list(await client.get("/memory")) == []


async def test_clearing_notifications_deletes_read_and_unread():
    async with client_app() as (client, app):
        service = app_state(app).notifications
        kept = await service.notify(OPERATOR_ID, "system", "read one")
        _ = await service.mark_read(OPERATOR_ID, [kept.id])
        _ = await service.notify(OPERATOR_ID, "system", "unread one")

        result = json_body(await client.post("/data/clear/notifications"))

        assert n(result["deleted"]) == 2
        page = json_body(await client.get("/notifications"))
        assert (page["items"], page["unreadCount"]) == ([], 0)


async def test_clearing_uploads_detaches_them_from_chats(monkeypatch: pytest.MonkeyPatch):
    patch_model_resolution(monkeypatch)
    async with client_app() as (client, _app):
        note = await _upload(client, "note.txt", "text/plain")
        conversation = await _chat(client, "read this", [note])

        result = json_body(await client.post("/data/clear/uploads"))

        assert n(result["deleted"]) == 1
        assert json_list(await client.get("/uploads")) == []
        detail = json_body(await client.get(f"/conversations/{conversation}"))
        assert all(obj(m)["attachment_ids"] in ([], None) for m in arr(detail["messages"]))


async def test_an_unknown_kind_is_rejected():
    async with client_app() as (client, _app):
        assert (await client.post("/data/clear/everything")).status_code == 422
