"""Messaging channels: the allowlist, one rolling chat per address, ``/new``, relaying
answers back, and approvals from a channel."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import pytest
from fastapi import FastAPI
from pydantic_ai import ModelMessage
from pydantic_ai.models.function import AgentInfo, FunctionModel

from core.exceptions import PermissionDeniedError
from routes import deps
from services.channels import (
    ApprovalRequest,
    ChannelHost,
    ChannelHub,
    ChannelLinks,
    Decision,
    PendingCall,
)
from services.conversations import ConversationStore
from services.registry import ModelRegistry

from ._helpers import client_app, register_stub_provider, settle, stub_resolution

OWNER_ID = "+1555"


@dataclass
class FakeChannel:
    key: str = "fake"
    label: str = "FAKE"
    fallback: str | None = None
    sent: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])
    asked: list[tuple[str, ApprovalRequest]] = field(
        default_factory=list[tuple[str, ApprovalRequest]]
    )
    host: ChannelHost | None = None

    async def start(self, hub: ChannelHost) -> None:
        self.host = hub

    async def stop(self) -> None:
        self.host = None

    async def send(self, address: str, text: str) -> None:
        self.sent.append((address, text))

    async def request_approval(self, address: str, request: ApprovalRequest) -> None:
        self.asked.append((address, request))

    async def approval_fallback(self) -> str | None:
        return self.fallback


def _answering(monkeypatch: pytest.MonkeyPatch, answer: str) -> None:
    async def stream_fn(_messages: list[ModelMessage], _info: AgentInfo) -> AsyncIterator[str]:
        yield answer

    async def resolve_detailed(self: ModelRegistry, _role: str, **_kwargs: object):
        return await stub_resolution(self, FunctionModel(stream_function=stream_fn))

    register_stub_provider(monkeypatch)
    monkeypatch.setattr(ModelRegistry, "resolve_detailed", resolve_detailed)


async def _hub(app: FastAPI, channel: FakeChannel) -> ChannelHub:
    hub = deps.service(app, ChannelHub)
    await hub.set_allowed_senders(channel.key, [OWNER_ID])
    await hub.attach(channel)
    return hub


async def test_a_message_starts_a_chat_and_its_answer_comes_back(monkeypatch: pytest.MonkeyPatch):
    _answering(monkeypatch, "pong")
    async with client_app() as (_client, app):
        channel = FakeChannel()
        hub = await _hub(app, channel)

        first = await hub.inbound("fake", OWNER_ID, "dm:1", "ping")
        assert first.outcome == "started" and first.conversation_id is not None
        await settle(app)
        assert channel.sent == [("dm:1", "pong")]

        again = await hub.inbound("fake", OWNER_ID, "dm:1", "and again")
        assert again.conversation_id == first.conversation_id
        await settle(app)
        assert len(channel.sent) == 2


async def test_new_starts_a_fresh_chat_at_that_address(monkeypatch: pytest.MonkeyPatch):
    _answering(monkeypatch, "ok")
    async with client_app() as (_client, app):
        hub = await _hub(app, FakeChannel())
        first = await hub.inbound("fake", OWNER_ID, "dm:1", "one")
        await settle(app)

        fresh = await hub.inbound("fake", OWNER_ID, "dm:1", "/new")
        assert fresh.outcome == "fresh" and fresh.reply is not None

        second = await hub.inbound("fake", OWNER_ID, "dm:1", "two")
        await settle(app)
        assert second.conversation_id not in (None, first.conversation_id)


async def test_a_stranger_is_ignored(monkeypatch: pytest.MonkeyPatch):
    _answering(monkeypatch, "ok")
    async with client_app() as (_client, app):
        channel = FakeChannel()
        hub = await _hub(app, channel)
        refused = await hub.inbound("fake", "someone-else", "dm:2", "hi")
        assert refused.outcome == "refused"
        await settle(app)
        assert channel.sent == []
        assert await deps.service(app, ChannelLinks).list(hub.owner_id) == []


async def test_an_answer_relays_only_where_the_chat_is_linked(monkeypatch: pytest.MonkeyPatch):
    _answering(monkeypatch, "ok")
    async with client_app() as (client, app):
        channel = FakeChannel()
        _ = await _hub(app, channel)
        sent = await client.post("/chat", json={"prompt": "from the app"})
        assert sent.status_code in (200, 201, 202), sent.text
        await settle(app)
        assert channel.sent == []


# --- approvals ------------------------------------------------------------------


def _request(run_id: str = "run-1") -> ApprovalRequest:
    call = PendingCall("call-1", "shell_run_on_host", "run `ls`", None)
    return ApprovalRequest(run_id, "conv-1", (call,), token="tok", questions=0)


@dataclass
class Answers:
    decided: list[tuple[str, tuple[Decision, ...]]] = field(
        default_factory=list[tuple[str, tuple[Decision, ...]]]
    )
    guarded: bool = False

    async def __call__(self, run_id: str, decisions: tuple[Decision, ...]) -> None:
        if self.guarded:
            raise PermissionDeniedError("guarded")
        self.decided.append((run_id, decisions))


async def _approval_hub(
    app: FastAPI, answers: Answers, pending: ApprovalRequest | None
) -> ChannelHub:
    async def no_send(prompt: str, conversation_id: str | None) -> tuple[str, bool]:
        raise AssertionError(f"unexpected send {prompt!r} {conversation_id!r}")

    hub = ChannelHub(
        owner_id=deps.service(app, ChannelHub).owner_id,
        links=deps.service(app, ChannelLinks),
        settings=deps.settings_store(app),
        conversations=deps.service(app, ConversationStore),
        send_chat=no_send,
        answer=answers,
        pending=lambda run_id: pending if pending and pending.run_id == run_id else None,
    )
    await hub.set_allowed_senders("fake", [OWNER_ID])
    return hub


async def test_a_button_approves_everything_pending():
    async with client_app() as (_client, app):
        answers = Answers()
        hub = await _approval_hub(app, answers, _request())
        said = await hub.decide("fake", OWNER_ID, "run-1", "tok", approve=True)
        assert said == "Approved."
        assert answers.decided == [("run-1", (Decision("call-1", True),))]


async def test_a_stale_or_foreign_button_decides_nothing():
    async with client_app() as (_client, app):
        answers = Answers()
        hub = await _approval_hub(app, answers, _request())
        stale = await hub.decide("fake", OWNER_ID, "run-1", "old", approve=True)
        foreign = await hub.decide("fake", OWNER_ID, "run-2", "tok", approve=True)
        assert stale == foreign == "Already decided."
        refused = await hub.decide("fake", "stranger", "run-1", "tok", approve=True)
        assert "can't" in refused
        assert answers.decided == []


async def test_a_guarded_call_points_to_the_app():
    async with client_app() as (_client, app):
        hub = await _approval_hub(app, Answers(guarded=True), _request())
        said = await hub.decide("fake", OWNER_ID, "run-1", "tok", approve=True)
        assert "in the app" in said


async def test_approvals_go_to_linked_addresses_or_the_fallback():
    async with client_app() as (_client, app):
        hub = await _approval_hub(app, Answers(), _request())
        channel = FakeChannel(fallback="dm:owner")
        await hub.attach(channel)

        await hub.request_approvals("run-1", "conv-1")
        assert [address for address, _ in channel.asked] == ["dm:owner"]

        await hub.links.link(hub.owner_id, "fake", "channel:9", "conv-1")
        await hub.request_approvals("run-1", "conv-1")
        assert [address for address, _ in channel.asked] == ["dm:owner", "channel:9"]
