"""How a restore copes with what happens around it: a failing step, a turn sent before it,
a busy thread, an evicted tree."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Literal

import pytest
from fastapi import FastAPI
from pydantic_ai import ModelRequest, ModelResponse, TextPart, ToolCallPart, UserPromptPart
from sqlmodel import Session, col, select

from core.db import in_session, init_db, make_engine
from core.vault import Vault
from models.conversation import Conversation, Message
from routes.deps import app_state
from runs import Run, RunRegistry, RunStream
from services.conversations import ConversationStore
from services.conversations.core import StoreCore
from services.parked_turns import SealedParkedStore, StoredPark

from ._helpers import await_parked, client_app, collect_sse_events, swap_tool_catalog
from ._json import json_body, s
from .test_approval_routes import danger_categories, install_sensitive_tool
from .test_parked_turns import await_row, park_chat, stored_parks


async def _until(check: Callable[[], bool], tries: int = 300) -> bool:
    for _ in range(tries):
        if check():
            return True
        await asyncio.sleep(0.01)
    return False


async def _notified(app: FastAPI) -> list[str]:
    items, _ = await app_state(app).notifications.list_notifications("operator", limit=50)
    return [n.kind for n in items]


async def _db(
    app: FastAPI, conversation_id: str
) -> tuple[list[tuple[str, str | None, int, str | None]], str | None]:
    def work(session: Session) -> tuple[list[tuple[str, str | None, int, str | None]], str | None]:
        rows = session.exec(
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(col(Message.seq))
        )
        conversation = session.get(Conversation, conversation_id)
        return (
            [(m.id, m.parent_id, m.seq, m.blocked_reason) for m in rows],
            conversation.active_leaf_id if conversation is not None else None,
        )

    return await in_session(app_state(app).db_engine, work)


async def _no_shutdown(_self: RunRegistry, _timeout_s: float = 5.0) -> None:
    pass


async def test_a_park_whose_restore_step_raises_is_ended_not_left_registered_orphaned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    install_sensitive_tool(monkeypatch)
    async with client_app(home=tmp_path) as (client, app):
        swap_tool_catalog(app, danger_categories())
        run_id, _call_id = await park_chat(client, app)

    async def boom(_self: RunRegistry, _run: Run) -> str | None:
        raise RuntimeError("the routine lookup failed")

    with monkeypatch.context() as failing:
        failing.setattr(RunRegistry, "restore_problem", boom)
        async with client_app(home=tmp_path) as (client, app):
            assert await _until(lambda: app_state(app).runs.parks_restored.is_set())
            assert await _until(
                lambda: (r := app_state(app).runs.get(run_id)) is not None and r.is_terminal
            )
            await await_row(app, run_id, present=False)
            assert (await _notified(app)).count("run_restore_failed") == 1


@pytest.mark.parametrize("how", ["approve", "cancel"])
async def test_acting_on_a_restored_park_never_reads_the_database_on_the_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, how: Literal["approve", "cancel"]
):
    install_sensitive_tool(monkeypatch)
    async with client_app(home=tmp_path) as (client, app):
        swap_tool_catalog(app, danger_categories())
        run_id, call_id = await park_chat(client, app)
    calls: list[str] = []
    real = StoreCore.hydrate_blocking

    def spy(self: StoreCore, conversation_id: str):
        calls.append(conversation_id)
        return real(self, conversation_id)

    monkeypatch.setattr(StoreCore, "hydrate_blocking", spy)
    async with client_app(home=tmp_path) as (client, app):
        swap_tool_catalog(app, danger_categories())
        _ = await await_parked(app, run_id)
        if how == "approve":
            decision = {"tool_call_id": call_id, "approved": True}
            _ = await client.post(f"/runs/{run_id}/approve", json={"decisions": [decision]})
            _ = await collect_sse_events(client, run_id)
        else:
            _ = await client.post(f"/runs/{run_id}/cancel")
        await app_state(app).conversations.flush()
    assert calls == []


async def test_a_tree_read_in_flight_does_not_replace_a_turn_recorded_meanwhile(tmp_path: Path):
    engine = make_engine(f"sqlite:///{tmp_path / 'app.db'}")
    init_db(engine)
    vault = Vault(tmp_path / "k.json")
    await vault.setup("pw")
    store = ConversationStore(engine, vault)
    await store.start()
    conversation_id = await store.create_conversation("operator")
    first = [
        ModelRequest(parts=[UserPromptPart("q0")]),
        ModelResponse(parts=[TextPart("a0")]),
    ]
    _ = store.record(conversation_id, first)
    await store.flush()
    store.evict_cache()

    reading = asyncio.create_task(store.tree(conversation_id))
    await asyncio.sleep(0)
    ids = store.record(
        conversation_id,
        [ModelRequest(parts=[UserPromptPart("q1")]), ModelResponse(parts=[TextPart("a1")])],
    )
    tree = await reading

    assert all(node_id in tree.nodes for node_id in ids)
    assert tree is store.cache_get(conversation_id)
    await store.stop()


async def test_a_park_beside_a_live_run_leaves_a_marker_a_notice_and_the_leaf_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    install_sensitive_tool(monkeypatch)
    with monkeypatch.context() as crash:
        crash.setattr(RunRegistry, "shutdown", _no_shutdown)
        async with client_app(home=tmp_path) as (client, app):
            swap_tool_catalog(app, danger_categories())
            run_id, _call_id = await park_chat(client, app)
            parked = app_state(app).runs.get(run_id)
            assert parked is not None and parked.conversation_id is not None
            conversation_id, prompt_id = parked.conversation_id, parked.prompt_message_id
            await app_state(app).conversations.flush()
            before = await _db(app, conversation_id)

    real = RunRegistry.active_run_for
    occupied = {"on": True}
    other = Run(id="other", kind="chat", owner_id="operator", stream=RunStream())

    def busy(self: RunRegistry, conversation: str, owner: str) -> Run | None:
        return other if occupied["on"] else real(self, conversation, owner)

    monkeypatch.setattr(RunRegistry, "active_run_for", busy)
    async with client_app(home=tmp_path) as (client, app):
        assert await _until(
            lambda: (r := app_state(app).runs.get(run_id)) is not None and r.is_terminal
        )
        occupied["on"] = False
        await app_state(app).conversations.flush()
        rows, leaf = await _db(app, conversation_id)
        assert leaf == before[1]
        stamped = {row[0]: row[3] for row in rows}
        assert stamped[s(prompt_id)] == "its conversation already has a run in progress"
        assert len(rows) == len(before[0])
        assert (await _notified(app)).count("run_restore_failed") == 1
        await await_row(app, run_id, present=False)

    async with client_app(home=tmp_path) as (client, app):
        await asyncio.sleep(0.2)
        assert app_state(app).runs.get(run_id) is None
        again, leaf_again = await _db(app, conversation_id)
        assert (again, leaf_again) == (rows, leaf)
        assert (await _notified(app)).count("run_restore_failed") == 1
        assert await stored_parks(app) == []


async def test_a_turn_sent_before_the_restore_does_not_take_the_parks_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    install_sensitive_tool(monkeypatch)
    async with client_app(home=tmp_path) as (client, app):
        swap_tool_catalog(app, danger_categories())
        run_id, call_id = await park_chat(client, app)
        parked = app_state(app).runs.get(run_id)
        assert parked is not None and parked.conversation_id is not None
        conversation_id = parked.conversation_id

    gate = asyncio.Event()
    real_load = SealedParkedStore.load

    async def delayed(self: SealedParkedStore) -> list[StoredPark]:
        _ = await gate.wait()
        return await real_load(self)

    monkeypatch.setattr(SealedParkedStore, "load", delayed)
    async with client_app(home=tmp_path) as (client, app):
        swap_tool_catalog(app, danger_categories())
        sending = asyncio.create_task(
            client.post("/chat", json={"prompt": "second", "conversation_id": conversation_id})
        )
        await asyncio.sleep(0.2)
        gate.set()
        _ = await sending
        _ = await await_parked(app, run_id)
        decision = {"tool_call_id": call_id, "approved": True}
        approved = await client.post(f"/runs/{run_id}/approve", json={"decisions": [decision]})
        assert json_body(approved) is not None
        _ = await collect_sse_events(client, run_id)
        await app_state(app).conversations.flush()

        tree = await app_state(app).conversations.tree(conversation_id)
        call_nodes = [
            node
            for node in tree.nodes.values()
            if isinstance(node.message, ModelResponse)
            and any(isinstance(p, ToolCallPart) for p in node.message.parts)
        ]
        assert call_nodes
        for node in call_nodes[:1]:
            seen: list[str] = []
            cursor = node.parent_id
            while cursor is not None:
                parent = tree.nodes[cursor]
                if isinstance(parent.message, ModelRequest):
                    seen.extend(
                        str(p.content)
                        for p in parent.message.parts
                        if isinstance(p, UserPromptPart)
                    )
                cursor = parent.parent_id
            assert seen == ["delete it"]
