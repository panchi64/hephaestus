"""Backend guards against a live run racing a second submission or a branch-tree
mutation on the same conversation (chat-03, chat-04, resume-02 backend half).

A live (queued/running/awaiting_input) run on a conversation must block every
leaf-moving branch op and a second ``/chat/regenerate``/``/chat/edit`` submission
with a clear 409 raised **before** any store mutation. A ``/chat`` POST is never
refused: it queues into the conversation's inbox, and whatever holds the thread takes it
next.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI, Request
from pydantic import JsonValue
from pydantic_ai import ModelMessage
from pydantic_ai.models.function import AgentInfo, FunctionModel

from routes.deps import OPERATOR_ID, app_state
from services.registry import ModelRegistry

from ._helpers import (
    await_parked,
    client_app,
    patch_model_resolution,
    register_stub_provider,
    settle,
    stub_resolution,
    swap_tool_catalog,
)
from ._json import arr, json_body, obj, s
from .test_approval_routes import danger_categories, install_sensitive_tool


async def _wait(app: FastAPI, run_id: str) -> None:
    run = app_state(app).runs.get(run_id)
    assert run is not None
    await run.wait()


def patch_hanging_model(
    monkeypatch: pytest.MonkeyPatch, hang: asyncio.Event, started: asyncio.Event
) -> None:
    """Point model resolution at a model whose answer hangs until ``hang`` is set —
    keeps the run ``running`` (never terminal) so a guard test can submit a second
    request while the first is still live. ``started`` fires once the model call has
    actually begun, so the test can wait past the request/response round-trip and the
    background task's own startup before asserting on registry/store state."""

    async def stream_fn(_messages: list[ModelMessage], _info: AgentInfo) -> AsyncIterator[str]:
        started.set()
        _ = await hang.wait()
        yield "done"

    def _model() -> FunctionModel:
        return FunctionModel(stream_function=stream_fn)

    async def resolve_detailed(self: ModelRegistry, _role: str, **_kwargs: object):
        return await stub_resolution(self, _model())

    register_stub_provider(monkeypatch)
    monkeypatch.setattr(ModelRegistry, "resolve_detailed", resolve_detailed)


async def _complete_first_turn(
    client: httpx.AsyncClient, prompt: str = "hello"
) -> tuple[str, dict[str, JsonValue]]:
    """Run one normal (fast ``TestModel``) turn to completion and return
    ``(conversation_id, assistant_message)`` — the fixture every guard test builds on."""
    created = json_body(await client.post("/chat", json={"prompt": prompt}))
    conv_id = s(created["conversation_id"])
    run_id = s(created["run_id"])
    async with client.stream("GET", f"/runs/{run_id}/events") as resp:
        async for _ in resp.aiter_lines():
            pass  # drain to run.ended so the turn is persisted
    detail = json_body(await client.get(f"/conversations/{conv_id}"))
    assistant = next(obj(m) for m in arr(detail["messages"]) if obj(m)["role"] == "assistant")
    return conv_id, assistant


async def test_second_chat_post_queues_into_live_run(monkeypatch: pytest.MonkeyPatch):
    hang, started = asyncio.Event(), asyncio.Event()
    patch_hanging_model(monkeypatch, hang, started)

    async with client_app() as (client, app):
        first = await client.post("/chat", json={"prompt": "hello"})
        assert first.status_code == 202
        first_body = json_body(first)
        conv_id = s(first_body["conversation_id"])
        _ = await started.wait()

        before = await client.get(f"/conversations/{conv_id}")

        second = await client.post("/chat", json={"prompt": "again", "conversation_id": conv_id})
        # Steering: the send is accepted into the live run — same run id, a queued
        # message id, and no second Run object ever created for the conversation.
        assert second.status_code == 202
        assert json_body(second)["run_id"] == first_body["run_id"]
        assert json_body(second)["queued_message_id"]

        # No store mutation until the run itself persists the turn (the live run's
        # `last_seq` does advance — queueing emits `message.queued` on its stream).
        after = await client.get(f"/conversations/{conv_id}")
        assert json_body(before)["messages"] == json_body(after)["messages"]
        live = app_state(app).runs.active_run_for(conv_id, OPERATOR_ID)
        assert live is not None and live.id == first_body["run_id"]

        hang.set()
        await _wait(app, s(first_body["run_id"]))


async def test_an_attachment_send_queues_and_opens_its_own_turn(monkeypatch: pytest.MonkeyPatch):
    # A send carrying attachments can't ride the running request, so it waits in the inbox
    # as a turn item and opens a turn of its own once the running one ends.
    hang, started = asyncio.Event(), asyncio.Event()
    patch_hanging_model(monkeypatch, hang, started)

    async with client_app() as (client, app):
        upload = await client.post(
            "/uploads", files={"file": ("note.txt", b"look at this", "text/plain")}
        )
        upload_id = s(json_body(upload)["id"])

        first = json_body(await client.post("/chat", json={"prompt": "hello"}))
        conv_id = s(first["conversation_id"])
        _ = await started.wait()

        second = await client.post(
            "/chat",
            json={
                "prompt": "again",
                "conversation_id": conv_id,
                "attachment_ids": [upload_id],
            },
        )
        assert second.status_code == 202
        queued_id = s(json_body(second)["queued_message_id"])
        inbox = app_state(app).runs.inbox(conv_id, OPERATOR_ID)
        [waiting] = inbox.messages
        assert (waiting.id, waiting.is_turn) == (queued_id, True)
        # Never injected into the running turn.
        assert inbox.drain() == []

        hang.set()
        await settle(app)
        assert inbox.messages == ()
        detail = json_body(await client.get(f"/conversations/{conv_id}"))
        users = [obj(m) for m in arr(detail["messages"]) if obj(m)["role"] == "user"]
        assert [s(m["content"]).split("\n")[0] for m in users] == ["hello", "again"]
        assert users[-1]["attachment_ids"] == [upload_id]


async def test_guard_releases_once_run_reaches_terminal(monkeypatch: pytest.MonkeyPatch):
    hang, started = asyncio.Event(), asyncio.Event()
    patch_hanging_model(monkeypatch, hang, started)

    async with client_app() as (client, app):
        first = json_body(await client.post("/chat", json={"prompt": "hello"}))
        conv_id = s(first["conversation_id"])
        _ = await started.wait()

        queued = await client.post("/chat", json={"prompt": "again", "conversation_id": conv_id})
        assert queued.status_code == 202
        assert json_body(queued)["queued_message_id"]

        hang.set()
        await settle(app)

        # Once the thread is idle a new send starts a fresh run (no queueing).
        accepted = await client.post("/chat", json={"prompt": "now", "conversation_id": conv_id})
        assert accepted.status_code == 202
        assert json_body(accepted)["run_id"] != first["run_id"]
        assert "queued_message_id" not in json_body(accepted)
        await settle(app)


async def test_regenerate_rejected_while_run_live_and_leaf_unmoved(
    monkeypatch: pytest.MonkeyPatch,
):
    patch_model_resolution(monkeypatch)
    async with client_app() as (client, app):
        conv_id, assistant = await _complete_first_turn(client)

        hang, started = asyncio.Event(), asyncio.Event()
        patch_hanging_model(monkeypatch, hang, started)

        regen1 = await client.post(
            "/chat/regenerate",
            json={"conversation_id": conv_id, "message_id": assistant["id"]},
        )
        assert regen1.status_code == 202
        _ = await started.wait()

        before = await client.get(f"/conversations/{conv_id}")

        regen2 = await client.post(
            "/chat/regenerate",
            json={"conversation_id": conv_id, "message_id": assistant["id"]},
        )
        assert regen2.status_code == 409

        after = await client.get(f"/conversations/{conv_id}")
        assert json_body(before) == json_body(after)  # the rejected regenerate moved nothing

        hang.set()
        await _wait(app, s(json_body(regen1)["run_id"]))


async def test_edit_rejected_while_run_live_and_leaf_unmoved(monkeypatch: pytest.MonkeyPatch):
    patch_model_resolution(monkeypatch)
    async with client_app() as (client, app):
        conv_id, _assistant = await _complete_first_turn(client)
        detail = json_body(await client.get(f"/conversations/{conv_id}"))
        user_msg = next(obj(m) for m in arr(detail["messages"]) if obj(m)["role"] == "user")

        hang, started = asyncio.Event(), asyncio.Event()
        patch_hanging_model(monkeypatch, hang, started)

        edit1 = await client.post(
            "/chat/edit",
            json={
                "conversation_id": conv_id,
                "message_id": user_msg["id"],
                "prompt": "hello v2",
            },
        )
        assert edit1.status_code == 202
        _ = await started.wait()

        before = await client.get(f"/conversations/{conv_id}")

        edit2 = await client.post(
            "/chat/edit",
            json={
                "conversation_id": conv_id,
                "message_id": user_msg["id"],
                "prompt": "hello v3",
            },
        )
        assert edit2.status_code == 409

        after = await client.get(f"/conversations/{conv_id}")
        assert json_body(before) == json_body(after)  # the rejected edit moved nothing

        hang.set()
        await _wait(app, s(json_body(edit1)["run_id"]))


async def test_branch_ops_rejected_while_run_live(monkeypatch: pytest.MonkeyPatch):
    patch_model_resolution(monkeypatch)
    async with client_app() as (client, app):
        conv_id, assistant = await _complete_first_turn(client)
        assistant_id = s(assistant["id"])

        hang, started = asyncio.Event(), asyncio.Event()
        patch_hanging_model(monkeypatch, hang, started)

        live = await client.post("/chat", json={"prompt": "again", "conversation_id": conv_id})
        assert live.status_code == 202
        _ = await started.wait()

        before = await client.get(f"/conversations/{conv_id}")

        version_resp = await client.post(
            f"/conversations/{conv_id}/messages/{assistant_id}/version",
            json={"index": 0},
        )
        assert version_resp.status_code == 409

        rewind_resp = await client.post(f"/conversations/{conv_id}/messages/{assistant_id}/rewind")
        assert rewind_resp.status_code == 409

        delete_resp = await client.delete(f"/conversations/{conv_id}/messages/{assistant_id}")
        assert delete_resp.status_code == 409

        after = await client.get(f"/conversations/{conv_id}")
        assert json_body(before) == json_body(after)  # none of the rejected ops mutated the tree

        # A pin is metadata, not a tree change, so it works while a turn streams.
        pin_resp = await client.post(
            f"/conversations/{conv_id}/messages/{assistant_id}/pin",
            json={"pinned": True},
        )
        assert pin_resp.status_code == 204

        hang.set()
        await _wait(app, s(json_body(live)["run_id"]))


def _patch_hanging_resolve(
    monkeypatch: pytest.MonkeyPatch, gate: asyncio.Event, entered: asyncio.Event
) -> None:
    """Model *resolution* (not the answer stream) hangs on ``gate`` until released —
    unlike ``patch_hanging_model`` (which hangs the streamed answer, long after a
    regenerate/edit has already repositioned the active leaf and submitted), this hangs
    inside ``resolve_models`` itself, the real `await` a second near-simultaneous
    regenerate/edit could otherwise slip through during. ``entered`` fires once the
    first caller is parked here, so a test can assert a second request is rejected
    without ever reaching (or needing to release) this gate."""

    def _model() -> FunctionModel:
        async def stream_fn(_messages: list[ModelMessage], _info: AgentInfo) -> AsyncIterator[str]:
            yield "hi"

        return FunctionModel(stream_function=stream_fn)

    async def resolve_detailed(self: ModelRegistry, _role: str, **_kwargs: object):
        entered.set()
        _ = await gate.wait()
        return await stub_resolution(self, _model())

    # Patched separately rather than left to delegate: the gate above would otherwise
    # also stall every background resolution this test isn't trying to hold.
    async def resolve_background(self: ModelRegistry, **_kwargs: object):
        return await stub_resolution(self, _model())

    register_stub_provider(monkeypatch)
    monkeypatch.setattr(ModelRegistry, "resolve_detailed", resolve_detailed)
    monkeypatch.setattr(ModelRegistry, "resolve_background", resolve_background)


async def test_concurrent_regenerates_only_one_proceeds_past_model_resolve(
    monkeypatch: pytest.MonkeyPatch,
):
    # chat-03 (regenerate half): the guard must close the gap between its own check and
    # `store.regenerate_point` moving the leaf, not just the gap before `submit`. Two
    # near-simultaneous regenerates on the same conversation must not both pass the
    # guard and both reposition the leaf before either registers a run.
    patch_model_resolution(monkeypatch)
    async with client_app() as (client, app):
        conv_id, assistant = await _complete_first_turn(client)

        gate, entered = asyncio.Event(), asyncio.Event()
        _patch_hanging_resolve(monkeypatch, gate, entered)

        before = await client.get(f"/conversations/{conv_id}")

        first_task = asyncio.ensure_future(
            client.post(
                "/chat/regenerate",
                json={"conversation_id": conv_id, "message_id": assistant["id"]},
            )
        )
        _ = await entered.wait()  # the first request is now parked inside `resolve_models`

        # A second regenerate while the first still holds the claim (mid-resolve, no
        # run registered yet) — must be rejected immediately, without ever reaching
        # (or needing) the still-hanging resolver.
        second = await client.post(
            "/chat/regenerate",
            json={"conversation_id": conv_id, "message_id": assistant["id"]},
        )
        assert second.status_code == 409
        assert not first_task.done()

        # Nothing mutated from the rejected request.
        after = await client.get(f"/conversations/{conv_id}")
        assert json_body(before) == json_body(after)

        gate.set()
        first = await first_task
        assert first.status_code == 202
        await _wait(app, s(json_body(first)["run_id"]))


async def test_concurrent_edit_and_regenerate_only_one_proceeds(monkeypatch: pytest.MonkeyPatch):
    # Same race, cross-endpoint: an edit and a regenerate targeting the same
    # conversation from two different requests must not both reposition the leaf.
    patch_model_resolution(monkeypatch)
    async with client_app() as (client, app):
        conv_id, assistant = await _complete_first_turn(client)
        detail = json_body(await client.get(f"/conversations/{conv_id}"))
        user_msg = next(obj(m) for m in arr(detail["messages"]) if obj(m)["role"] == "user")

        gate, entered = asyncio.Event(), asyncio.Event()
        _patch_hanging_resolve(monkeypatch, gate, entered)

        regen_task = asyncio.ensure_future(
            client.post(
                "/chat/regenerate",
                json={"conversation_id": conv_id, "message_id": assistant["id"]},
            )
        )
        _ = await entered.wait()

        edit_resp = await client.post(
            "/chat/edit",
            json={
                "conversation_id": conv_id,
                "message_id": user_msg["id"],
                "prompt": "hello v2",
            },
        )
        assert edit_resp.status_code == 409
        assert not regen_task.done()

        gate.set()
        regen = await regen_task
        assert regen.status_code == 202
        await _wait(app, s(json_body(regen)["run_id"]))


async def test_delete_message_purge_claims_against_a_concurrent_chat_submission(
    monkeypatch: pytest.MonkeyPatch,
):
    # `image_orphans` awaits real DB round trips before the leaf-moving
    # `store.delete_message` runs — a concurrent /chat submission landing in that window
    # must not be allowed to register a run that the delete then mutates the tree under.
    patch_model_resolution(monkeypatch)
    async with client_app() as (client, app):
        conv_id, assistant = await _complete_first_turn(client)

        import routes.conversation_teardown as teardown

        gate, entered = asyncio.Event(), asyncio.Event()
        original = teardown.image_orphans

        async def _hanging_image_orphans(
            request: Request, conversation_id: str, *, message_id: str | None
        ) -> list[str]:
            entered.set()
            _ = await gate.wait()
            return await original(request, conversation_id, message_id=message_id)

        monkeypatch.setattr(teardown, "image_orphans", _hanging_image_orphans)

        delete_task = asyncio.ensure_future(
            client.delete(f"/conversations/{conv_id}/messages/{s(assistant['id'])}")
        )
        _ = await entered.wait()  # the delete is now parked inside `image_orphans`

        # A send during the delete is not refused: it queues behind the claim.
        concurrent_chat = await client.post(
            "/chat", json={"prompt": "again", "conversation_id": conv_id}
        )
        assert concurrent_chat.status_code == 202
        assert json_body(concurrent_chat)["run_id"] is None

        gate.set()
        delete_resp = await delete_task
        assert delete_resp.status_code == 200

        # Releasing the claim started a run for it.
        await settle(app)
        assert app_state(app).runs.inbox(conv_id, OPERATOR_ID).messages == ()
        detail = json_body(await client.get(f"/conversations/{conv_id}"))
        users = [s(obj(m)["content"]) for m in arr(detail["messages"]) if obj(m)["role"] == "user"]
        assert users[-1] == "again"


async def test_awaiting_input_run_queues_a_new_chat_submission(monkeypatch: pytest.MonkeyPatch):
    # A parked (awaiting_input) run is not terminal — `active_run_for` still reports
    # it, so a new plain-text send queues into it (injected on resume) rather than
    # starting a second run; the run itself stays parked.
    install_sensitive_tool(monkeypatch)
    async with client_app() as (client, app):
        swap_tool_catalog(app, danger_categories())
        first = json_body(await client.post("/chat", json={"prompt": "delete it"}))
        conv_id = s(first["conversation_id"])
        run = await await_parked(app, s(first["run_id"]))
        assert run.status == "awaiting_input"

        queued = await client.post("/chat", json={"prompt": "again", "conversation_id": conv_id})
        assert queued.status_code == 202
        assert json_body(queued)["run_id"] == first["run_id"]
        assert json_body(queued)["queued_message_id"]
        assert run.status == "awaiting_input"
        assert [m.text for m in run.pending_messages] == ["again"]
