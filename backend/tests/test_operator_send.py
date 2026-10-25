"""The operator acting from outside the app runs the app's own send and approval routes."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
from fastapi import HTTPException
from pydantic_ai import ModelMessage
from pydantic_ai.models.function import AgentInfo, FunctionModel

from core.exceptions import PermissionDeniedError
from routes.chat.operator_send import answer_approval, operator_request, send_operator_message
from routes.chat.schemas import ChatCreate, ChatCreated, ChatQueued
from routes.deps import require_operator
from routes.runs import ApprovalDecisions
from services.registry import ModelRegistry

from ._helpers import client_app, register_stub_provider, settle, stub_resolution


def _gated_model(monkeypatch: pytest.MonkeyPatch, gate: asyncio.Event) -> asyncio.Event:
    started = asyncio.Event()

    async def stream_fn(_messages: list[ModelMessage], _info: AgentInfo) -> AsyncIterator[str]:
        started.set()
        _ = await gate.wait()
        yield "ok"

    async def resolve_detailed(self: ModelRegistry, _role: str, **_kwargs: object):
        return await stub_resolution(self, FunctionModel(stream_function=stream_fn))

    register_stub_provider(monkeypatch)
    monkeypatch.setattr(ModelRegistry, "resolve_detailed", resolve_detailed)
    return started


async def test_a_message_from_outside_starts_a_turn_then_queues_behind_it(
    monkeypatch: pytest.MonkeyPatch,
):
    gate = asyncio.Event()
    started = _gated_model(monkeypatch, gate)
    async with client_app() as (_client, app):
        first = await send_operator_message(app, ChatCreate(prompt="hello"))
        assert isinstance(first, ChatCreated)
        _ = await started.wait()

        second = await send_operator_message(
            app, ChatCreate(prompt="also", conversation_id=first.conversation_id)
        )
        assert isinstance(second, ChatQueued)
        assert second.run_id == first.run_id

        gate.set()
        await settle(app)


async def test_an_approval_from_outside_goes_through_the_route():
    async with client_app() as (_client, app):
        with pytest.raises(HTTPException) as refused:
            _ = await answer_approval(app, "no-such-run", ApprovalDecisions())
        assert refused.value.status_code == 404


async def test_a_request_from_outside_is_not_the_operators_session():
    # A guarded approval needs the operator's own session; a channel never carries one.
    async with client_app(auth_enabled=True, passphrase=None) as (_client, app):
        with pytest.raises(PermissionDeniedError):
            require_operator(operator_request(app, "/runs/x/approve"))
