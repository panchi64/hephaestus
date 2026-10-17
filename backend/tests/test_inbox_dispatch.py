"""Starting turns from the inbox without losing, refusing or duplicating a message.

The dispatcher races requests that claim a thread, shutdown, and the operator's own edits;
each of these holds one of the ways that race used to lose a message.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import cast

import pytest
from fastapi import FastAPI, HTTPException, Request
from pydantic_ai import ModelMessage, ModelRequest, UserPromptPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from agent.prelude import TurnSetup, record_prompt
from harness.manifests._subagent_wake import SubagentWake
from routes.chat.turn import TurnModels
from routes.deps import OPERATOR_ID, Source, app_state
from runs import STOP_NOTICE, Run, TurnPayload
from services.commands.spec import Invocation
from services.conversations import ConversationStore
from services.notification_view import NotificationView
from services.notifications import NotificationService
from services.registry import ModelRegistry

from ._helpers import client_app, register_stub_provider, settle, stub_resolution
from ._json import json_body, s


def _texts(request: ModelRequest) -> list[str]:
    texts: list[str] = []
    for part in request.parts:
        if isinstance(part, UserPromptPart):
            content = part.content if isinstance(part.content, list) else [part.content]
            texts.extend(c for c in content if isinstance(c, str))
    return texts


def _recording_model(
    monkeypatch: pytest.MonkeyPatch, seen: list[list[str]], gate: asyncio.Event
) -> asyncio.Event:
    """Records the user texts of every request and holds its answer on ``gate``. Returns
    an event set when the first request arrives."""
    started = asyncio.Event()

    async def stream_fn(messages: list[ModelMessage], _info: AgentInfo) -> AsyncIterator[str]:
        last = messages[-1]
        assert isinstance(last, ModelRequest)
        seen.append(_texts(last))
        started.set()
        _ = await gate.wait()
        yield "ok"

    async def resolve_detailed(self: ModelRegistry, _role: str, **_kwargs: object):
        return await stub_resolution(self, FunctionModel(stream_function=stream_fn))

    register_stub_provider(monkeypatch)
    monkeypatch.setattr(ModelRegistry, "resolve_detailed", resolve_detailed)
    return started


def _said(seen: list[list[str]], text: str) -> bool:
    return any(text in each for request in seen for each in request)


def _wake(app: FastAPI) -> SubagentWake:
    wake = cast(object, app.state.subagent_wake)
    assert isinstance(wake, SubagentWake)
    return wake


async def test_shutdown_starts_no_turn_and_keeps_the_queue(monkeypatch: pytest.MonkeyPatch):
    seen: list[list[str]] = []
    gate = asyncio.Event()
    started = _recording_model(monkeypatch, seen, gate)
    async with client_app() as (client, app):
        first = json_body(await client.post("/chat", json={"prompt": "hello"}))
        conversation_id = s(first["conversation_id"])
        _ = await started.wait()
        body = {"prompt": "later", "conversation_id": conversation_id}
        assert json_body(await client.post("/chat", json=body))["queued_message_id"]
        runs = app_state(app).runs
        runs_before = len(runs.list())

        await runs.shutdown(1.0)
        gate.set()
        await settle(app)

        assert len(runs.list()) == runs_before
        assert len(seen) == 1
        inbox = runs.inbox(conversation_id, OPERATOR_ID)
        assert [m.text for m in inbox.messages] == ["later"]


async def test_a_wake_under_a_sends_claim_leaves_the_send_a_turn(monkeypatch: pytest.MonkeyPatch):
    seen: list[list[str]] = []
    gate = asyncio.Event()
    gate.set()
    started = _recording_model(monkeypatch, seen, gate)
    real = ConversationStore.set_permission_level

    async with client_app() as (client, app):
        first = json_body(await client.post("/chat", json={"prompt": "hello"}))
        conversation_id = s(first["conversation_id"])
        _ = await started.wait()
        await settle(app)

        async def deliver_mid_window(self: ConversationStore, cid: str, level: str):
            _ = await _wake(app).deliver(OPERATOR_ID, cid, "found it")
            return await real(self, cid, level)

        monkeypatch.setattr(ConversationStore, "set_permission_level", deliver_mid_window)
        sent = await client.post(
            "/chat",
            json={
                "prompt": "and now",
                "conversation_id": conversation_id,
                "permission_level": "edit",
            },
        )
        await settle(app)

        assert sent.status_code == 202
        assert _said(seen, "and now")
        assert _said(seen, "found it")
        assert app_state(app).runs.inbox(conversation_id, OPERATOR_ID).messages == ()


async def test_a_send_that_loses_its_claim_to_a_run_queues_instead_of_409(
    monkeypatch: pytest.MonkeyPatch,
):
    seen: list[list[str]] = []
    gate = asyncio.Event()
    gate.set()
    started = _recording_model(monkeypatch, seen, gate)
    real = ConversationStore.set_permission_level
    release = asyncio.Event()

    async def hold(_run: Run) -> None:
        _ = await release.wait()

    async with client_app() as (client, app):
        first = json_body(await client.post("/chat", json={"prompt": "hello"}))
        conversation_id = s(first["conversation_id"])
        _ = await started.wait()
        await settle(app)

        levels: list[str] = []

        async def start_run_mid_window(self: ConversationStore, cid: str, level: str):
            if not levels:
                _ = app_state(app).runs.submit(
                    kind="wake", owner_id=OPERATOR_ID, orchestrator=hold, conversation_id=cid
                )
            levels.append(level)
            return await real(self, cid, level)

        monkeypatch.setattr(ConversationStore, "set_permission_level", start_run_mid_window)
        sent = await client.post(
            "/chat",
            json={
                "prompt": "and now",
                "conversation_id": conversation_id,
                "permission_level": "edit",
            },
        )
        assert sent.status_code == 202
        assert json_body(sent)["queued_message_id"]
        # The level moved for a turn this send did not get, so it was put back.
        assert levels[0] == "edit" and levels[-1] != "edit"
        binding = await app_state(app).conversations.binding(conversation_id)
        assert binding.permission != "edit"

        release.set()
        await settle(app)
        assert _said(seen, "and now")


async def test_a_send_queued_behind_a_fold_after_a_stop_is_not_stranded(
    monkeypatch: pytest.MonkeyPatch,
):
    seen: list[list[str]] = []
    gate = asyncio.Event()
    started = _recording_model(monkeypatch, seen, gate)
    fold_gate = asyncio.Event()
    fold_started = asyncio.Event()

    async def gated_fold(_run: Run, _request: Request, _pick: object) -> None:
        fold_started.set()
        _ = await fold_gate.wait()

    monkeypatch.setattr("routes.conversations._run_manual_fold", gated_fold)
    async with client_app() as (client, app):
        first = json_body(await client.post("/chat", json={"prompt": "hello"}))
        conversation_id = s(first["conversation_id"])
        _ = await started.wait()
        stopped = await client.post(f"/runs/{s(first['run_id'])}/cancel", json={"hold_inbox": True})
        assert stopped.status_code == 202
        gate.set()
        await settle(app)
        inbox = app_state(app).runs.inbox(conversation_id, OPERATOR_ID)
        assert inbox.waiting_for_operator

        folding = await client.post(f"/conversations/{conversation_id}/compact")
        assert folding.status_code == 202
        _ = await fold_started.wait()
        body = {"prompt": "do this next", "conversation_id": conversation_id}
        assert json_body(await client.post("/chat", json=body))["queued_message_id"]
        assert not inbox.waiting_for_operator

        fold_gate.set()
        await settle(app)

        assert _said(seen, "do this next")


async def test_a_withdraw_during_a_dispatch_is_told_it_is_too_late(
    monkeypatch: pytest.MonkeyPatch,
):
    seen: list[list[str]] = []
    gate = asyncio.Event()
    started = _recording_model(monkeypatch, seen, gate)
    entered = asyncio.Event()
    proceed = asyncio.Event()
    from routes.chat import queued, submit

    async def slow_models(
        request: Source, conversation_id: str, endpoint_id: str | None, model: str | None
    ) -> TurnModels:
        entered.set()
        _ = await proceed.wait()
        return await submit.thread_models(request, conversation_id, endpoint_id, model)

    async with client_app() as (client, app):
        first = json_body(await client.post("/chat", json={"prompt": "hello"}))
        conversation_id = s(first["conversation_id"])
        _ = await started.wait()
        queued_turn = (
            app_state(app)
            .runs.inbox(conversation_id, OPERATOR_ID)
            .enqueue("queued text", payload=TurnPayload(file_refs=("x",)))
        )
        message_id = queued_turn.id

        monkeypatch.setattr(queued, "thread_models", slow_models)
        gate.set()
        _ = await entered.wait()
        withdrawn = await client.delete(f"/conversations/{conversation_id}/inbox/{message_id}")
        assert withdrawn.status_code == 404
        proceed.set()
        await settle(app)

        assert _said(seen, "queued text")


async def test_an_operator_message_that_cannot_start_is_held_and_the_rest_flow(
    monkeypatch: pytest.MonkeyPatch,
):
    seen: list[list[str]] = []
    gate = asyncio.Event()
    started = _recording_model(monkeypatch, seen, gate)
    notices: list[str] = []

    real_notify = NotificationService.notify

    async def notify(
        self: NotificationService,
        owner_id: str,
        kind: str,
        title: str,
        body: str | None = None,
        **kwargs: str | None,
    ) -> NotificationView:
        notices.append(title)
        return await real_notify(self, owner_id, kind, title, body, **kwargs)

    async def models(*_args: object, **_kwargs: object):
        raise HTTPException(status_code=404, detail="endpoint not found")

    monkeypatch.setattr(NotificationService, "notify", notify)
    async with client_app() as (client, app):
        first = json_body(await client.post("/chat", json={"prompt": "hello"}))
        conversation_id = s(first["conversation_id"])
        _ = await started.wait()
        inbox = app_state(app).runs.inbox(conversation_id, OPERATOR_ID)
        stuck = inbox.enqueue("cannot start", payload=TurnPayload(file_refs=("x",)))
        report = inbox.enqueue("found it", source="subagent")

        monkeypatch.setattr("routes.chat.queued.thread_models", models)
        gate.set()
        await settle(app)

        held = inbox.get(stuck.id)
        assert held is not None and held.held
        assert notices.count("Queued message could not start") == 1
        assert inbox.get(report.id) is None
        assert _said(seen, "found it")


async def test_reports_no_turn_can_carry_wait_for_the_operator_and_go_with_their_turn(
    monkeypatch: pytest.MonkeyPatch,
):
    seen: list[list[str]] = []
    gate = asyncio.Event()
    gate.set()
    started = _recording_model(monkeypatch, seen, gate)
    notices: list[str] = []
    real_notify = NotificationService.notify

    async def notify(
        self: NotificationService,
        owner_id: str,
        kind: str,
        title: str,
        body: str | None = None,
        **kwargs: str | None,
    ) -> NotificationView:
        notices.append(title)
        return await real_notify(self, owner_id, kind, title, body, **kwargs)

    async def no_models(*_args: object, **_kwargs: object):
        raise HTTPException(status_code=404, detail="endpoint not found")

    monkeypatch.setattr(NotificationService, "notify", notify)
    async with client_app() as (client, app):
        first = json_body(await client.post("/chat", json={"prompt": "hello"}))
        conversation_id = s(first["conversation_id"])
        _ = await started.wait()
        await settle(app)
        # Only the dispatcher's own resolution fails: the operator's turn still runs.
        monkeypatch.setattr("harness.manifests._inbox_dispatch.resolve_turn_models", no_models)
        dispatcher = app_state(app).inbox_dispatcher
        assert dispatcher is not None

        _ = await _wake(app).deliver(OPERATOR_ID, conversation_id, "found it")
        _ = await _wake(app).deliver(OPERATOR_ID, conversation_id, "found more")
        await settle(app)

        assert dispatcher.stalled(conversation_id)
        assert notices.count("Agents paused for you") == 1
        inbox = app_state(app).runs.inbox(conversation_id, OPERATOR_ID)
        assert len(inbox.messages) == 2

        body = {"prompt": "what did they find", "conversation_id": conversation_id}
        _ = await client.post("/chat", json=body)
        await settle(app)

        assert _said(seen, "found it") and _said(seen, "found more")
        assert inbox.messages == ()
        assert not dispatcher.stalled(conversation_id)


async def test_a_queued_turn_item_runs_at_the_level_it_was_sent_with(
    monkeypatch: pytest.MonkeyPatch,
):
    seen: list[list[str]] = []
    gate = asyncio.Event()
    started = _recording_model(monkeypatch, seen, gate)
    async with client_app() as (client, app):
        first = json_body(await client.post("/chat", json={"prompt": "hello"}))
        conversation_id = s(first["conversation_id"])
        _ = await started.wait()
        _ = (
            app_state(app)
            .runs.inbox(conversation_id, OPERATOR_ID)
            .enqueue("with a file", payload=TurnPayload(file_refs=("x",), permission_level="edit"))
        )

        gate.set()
        await settle(app)

        assert _said(seen, "with a file")
        binding = await app_state(app).conversations.binding(conversation_id)
        assert binding.permission == "edit"


async def test_settle_names_the_run_a_dispatch_started(monkeypatch: pytest.MonkeyPatch):
    seen: list[list[str]] = []
    gate = asyncio.Event()
    started = _recording_model(monkeypatch, seen, gate)
    async with client_app() as (client, app):
        first = json_body(await client.post("/chat", json={"prompt": "hello"}))
        conversation_id = s(first["conversation_id"])
        _ = await started.wait()
        _ = (
            app_state(app)
            .runs.inbox(conversation_id, OPERATOR_ID)
            .enqueue("next", payload=TurnPayload(file_refs=("x",)))
        )

        # A plain cancel holds nothing, so the queued turn item starts as the run ends. A
        # client asks once the run has told it so.
        ending = app_state(app).runs.get(s(first["run_id"]))
        assert ending is not None
        _ = await client.post(f"/runs/{ending.id}/cancel")
        await ending.wait()
        settled = json_body(await client.post(f"/conversations/{conversation_id}/inbox/settle"))

        live = app_state(app).runs.active_run_for(conversation_id, OPERATOR_ID)
        assert live is not None and live.id != first["run_id"]
        assert settled["run_id"] == live.id
        assert settled["messages"] == []
        gate.set()
        await settle(app)


async def test_a_stop_notice_outlives_a_turn_that_failed_before_it_began(
    monkeypatch: pytest.MonkeyPatch,
):
    seen: list[list[str]] = []
    gate = asyncio.Event()
    started = _recording_model(monkeypatch, seen, gate)
    async with client_app() as (client, app):
        first = json_body(await client.post("/chat", json={"prompt": "hello"}))
        conversation_id = s(first["conversation_id"])
        _ = await started.wait()
        _ = await client.post(f"/runs/{s(first['run_id'])}/cancel", json={"hold_inbox": True})
        gate.set()
        await settle(app)

        from agent import engine

        real = record_prompt
        failures: list[str] = []

        def fail_once(
            setup: TurnSetup,
            run: Run,
            *,
            prompt: str | None,
            command: Invocation | None,
            store: ConversationStore | None,
            conversation_id: str | None,
        ) -> None:
            if not failures:
                failures.append("failed")
                raise RuntimeError("store unavailable")
            real(
                setup,
                run,
                prompt=prompt,
                command=command,
                store=store,
                conversation_id=conversation_id,
            )

        monkeypatch.setattr(engine, "record_prompt", fail_once)
        body = {"prompt": "first try", "conversation_id": conversation_id}
        _ = await client.post("/chat", json=body)
        await settle(app)
        _ = await client.post("/chat", json={**body, "prompt": "second try"})
        await settle(app)

        assert failures == ["failed"]
        assert _said(seen, STOP_NOTICE)
