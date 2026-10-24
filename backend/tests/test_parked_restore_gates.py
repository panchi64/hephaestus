"""The gates around a restore: requests held until the parks are back, a run that starts
during the restore, a park without its prompt, and the blocking-read fallback's failures."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from pathlib import Path

import httpx
import pytest
from pydantic_ai import ModelRequest, ModelResponse, TextPart, UserPromptPart
from sqlalchemy import Engine
from sqlalchemy.exc import OperationalError
from sqlmodel import Session, col, delete

import core.db as db
import routes.deps as route_deps
from core.db import in_session, init_db, make_engine
from core.vault import Vault
from models.conversation import Conversation, Message
from routes.deps import app_state
from runs import Run, RunRegistry, RunStatus
from services.conversations import ConversationStore
from services.conversations import core as conversations_core
from services.conversations.core import ConversationUnavailableError, StoreCore
from services.parked_turns import SealedParkedStore, StoredPark

from ._helpers import await_parked, client_app, collect_sse_events, swap_tool_catalog
from .test_approval_routes import danger_categories, install_sensitive_tool
from .test_parked_turns import await_row, park_chat

Request = Callable[[httpx.AsyncClient, str, str], Coroutine[object, object, httpx.Response]]


def _delayed_load(monkeypatch: pytest.MonkeyPatch, gate: asyncio.Event) -> None:
    real_load = SealedParkedStore.load

    async def delayed(self: SealedParkedStore) -> list[StoredPark]:
        _ = await gate.wait()
        return await real_load(self)

    monkeypatch.setattr(SealedParkedStore, "load", delayed)


async def _store(tmp_path: Path) -> tuple[ConversationStore, Engine]:
    engine = make_engine(f"sqlite:///{tmp_path / 'app.db'}")
    init_db(engine)
    vault = Vault(tmp_path / "k.json")
    await vault.setup("pw")
    store = ConversationStore(engine, vault)
    await store.start()
    return store, engine


async def _evicted_thread(tmp_path: Path) -> tuple[ConversationStore, Engine, str]:
    store, engine = await _store(tmp_path)
    conversation_id = await store.create_conversation("operator")
    _ = store.record(
        conversation_id,
        [ModelRequest(parts=[UserPromptPart("q")]), ModelResponse(parts=[TextPart("a")])],
    )
    await store.flush()
    store.evict_cache()
    return store, engine, conversation_id


def _locked() -> OperationalError:
    return OperationalError("SELECT 1", {}, Exception("database is locked"))


_REQUESTS: dict[str, Request] = {
    "delete_message": lambda c, cid, mid: c.delete(f"/conversations/{cid}/messages/{mid}"),
    "rewind": lambda c, cid, mid: c.post(f"/conversations/{cid}/messages/{mid}/rewind"),
    "version": lambda c, cid, mid: c.post(
        f"/conversations/{cid}/messages/{mid}/version", json={"index": 0}
    ),
    "fork": lambda c, cid, mid: c.post(f"/conversations/{cid}/messages/{mid}/fork"),
    "compact": lambda c, cid, _mid: c.post(f"/conversations/{cid}/compact"),
    "revert_files": lambda c, cid, _mid: c.post(f"/conversations/{cid}/changes/revert", json={}),
    "rewind_files": lambda c, cid, mid: c.post(
        f"/conversations/{cid}/changes/rewind", json={"messageId": mid, "previewToken": "x"}
    ),
    "delete_conversation": lambda c, cid, _mid: c.delete(f"/conversations/{cid}"),
    "regenerate": lambda c, cid, mid: c.post(
        "/chat/regenerate", json={"conversation_id": cid, "message_id": mid}
    ),
    "edit": lambda c, cid, mid: c.post(
        "/chat/edit", json={"conversation_id": cid, "message_id": mid, "prompt": "changed"}
    ),
}


@pytest.mark.parametrize("name", sorted(_REQUESTS))
async def test_a_request_that_changes_a_thread_waits_for_the_parks_to_be_restored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
):
    install_sensitive_tool(monkeypatch)
    async with client_app(home=tmp_path) as (client, app):
        swap_tool_catalog(app, danger_categories())
        run_id, _call_id = await park_chat(client, app)
        parked = app_state(app).runs.get(run_id)
        assert parked is not None and parked.conversation_id is not None
        conversation_id, prompt_id = parked.conversation_id, str(parked.prompt_message_id)

    gate = asyncio.Event()
    _delayed_load(monkeypatch, gate)
    async with client_app(home=tmp_path) as (client, app):
        swap_tool_catalog(app, danger_categories())
        sending = asyncio.create_task(_REQUESTS[name](client, conversation_id, prompt_id))
        await asyncio.sleep(0.2)
        assert not sending.done()
        gate.set()
        response = await sending
        restored = await await_parked(app, run_id)
        assert response.status_code == 409
        assert restored.status is RunStatus.awaiting_input


async def test_a_send_that_outwaits_the_cap_is_refused_with_a_503(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    install_sensitive_tool(monkeypatch)
    async with client_app(home=tmp_path) as (client, app):
        swap_tool_catalog(app, danger_categories())
        run_id, _call_id = await park_chat(client, app)
        parked = app_state(app).runs.get(run_id)
        assert parked is not None and parked.conversation_id is not None
        conversation_id = parked.conversation_id

    _delayed_load(monkeypatch, asyncio.Event())
    monkeypatch.setattr(route_deps, "RESTORE_WAIT_S", 0.05)
    caplog.set_level(logging.WARNING)
    async with client_app(home=tmp_path) as (client, app):
        swap_tool_catalog(app, danger_categories())
        sent = await client.post(
            "/chat", json={"prompt": "again", "conversation_id": conversation_id}
        )
        assert sent.status_code == 503
        assert "still being restored" in sent.text
    assert any("stored parks not restored" in r.getMessage() for r in caplog.records)


async def test_a_run_started_while_the_restore_loads_the_thread_ends_the_park(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    install_sensitive_tool(monkeypatch)
    async with client_app(home=tmp_path) as (client, app):
        swap_tool_catalog(app, danger_categories())
        run_id, _call_id = await park_chat(client, app)
        parked = app_state(app).runs.get(run_id)
        assert parked is not None and parked.conversation_id is not None
        conversation_id = parked.conversation_id

    entered, release = asyncio.Event(), asyncio.Event()
    real_tree = ConversationStore.tree
    first = {"seen": False}

    async def slow_tree(self: ConversationStore, conversation: str):
        if conversation == conversation_id and not first["seen"]:
            first["seen"] = True
            entered.set()
            _ = await release.wait()
        return await real_tree(self, conversation)

    monkeypatch.setattr(ConversationStore, "tree", slow_tree)
    async with client_app(home=tmp_path) as (client, app):
        _ = await asyncio.wait_for(entered.wait(), 5)
        hold = asyncio.Event()

        async def orchestrator(_run: Run) -> None:
            _ = await hold.wait()

        live = app_state(app).runs.submit(
            kind="compaction",
            owner_id="operator",
            orchestrator=orchestrator,
            conversation_id=conversation_id,
        )
        release.set()
        assert await _until(lambda: app_state(app).runs.parks_restored.is_set())
        ended = app_state(app).runs.get(run_id)
        assert ended is not None and ended.status is RunStatus.blocked
        assert [
            r.id
            for r in app_state(app).runs.list()
            if r.conversation_id == conversation_id and not r.is_terminal
        ] == [live.id]
        hold.set()


async def _until(check: Callable[[], bool], tries: int = 300) -> bool:
    for _ in range(tries):
        if check():
            return True
        await asyncio.sleep(0.01)
    return False


async def test_a_park_whose_prompt_is_gone_from_the_thread_is_ended_and_announced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    install_sensitive_tool(monkeypatch)
    async with client_app(home=tmp_path) as (client, app):
        swap_tool_catalog(app, danger_categories())
        run_id, _call_id = await park_chat(client, app)
        parked = app_state(app).runs.get(run_id)
        assert parked is not None and parked.conversation_id is not None
        conversation_id, prompt_id = parked.conversation_id, str(parked.prompt_message_id)
        await app_state(app).conversations.flush()

        def drop(session: Session) -> None:
            conversation = session.get(Conversation, conversation_id)
            assert conversation is not None
            conversation.active_leaf_id = None
            session.add(conversation)
            session.flush()
            _ = session.exec(delete(Message).where(col(Message.id) == prompt_id))

        await in_session(app_state(app).db_engine, drop)

    async with client_app(home=tmp_path) as (client, app):
        assert await _until(
            lambda: (r := app_state(app).runs.get(run_id)) is not None and r.is_terminal
        )
        ended = app_state(app).runs.get(run_id)
        assert ended is not None and ended.status is RunStatus.blocked
        await await_row(app, run_id, present=False)
        items, _ = await app_state(app).notifications.list_notifications("operator", limit=50)
        assert [n.kind for n in items].count("run_restore_failed") == 1


async def test_the_thread_stays_pinned_from_the_approval_until_the_run_ends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    install_sensitive_tool(monkeypatch)
    async with client_app(home=tmp_path) as (client, app):
        swap_tool_catalog(app, danger_categories())
        run_id, call_id = await park_chat(client, app)

    unpinned_while: list[bool] = []
    real_unpin = StoreCore.unpin

    def spy(self: StoreCore, conversation_id: str) -> None:
        run = seen.get("registry")
        live = run.get(run_id) if run is not None else None
        unpinned_while.append(live is not None and live.is_terminal)
        real_unpin(self, conversation_id)

    seen: dict[str, RunRegistry] = {}
    monkeypatch.setattr(StoreCore, "unpin", spy)
    async with client_app(home=tmp_path) as (client, app):
        swap_tool_catalog(app, danger_categories())
        seen["registry"] = app_state(app).runs
        _ = await await_parked(app, run_id)
        decision = {"tool_call_id": call_id, "approved": True}
        _ = await client.post(f"/runs/{run_id}/approve", json={"decisions": [decision]})
        _ = await collect_sse_events(client, run_id)
    assert unpinned_while and all(unpinned_while)


async def test_a_thread_that_cannot_be_opened_raises_instead_of_recording_into_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    store, _engine, conversation_id = await _evicted_thread(tmp_path)
    store.vault.lock()
    with pytest.raises(ConversationUnavailableError):
        _ = store.hydrate_blocking(conversation_id)
    store.vault.lock()

    def busy(_engine: Engine, _work: object) -> object:
        raise _locked()

    monkeypatch.setattr(conversations_core, "read_blocking", busy)
    with pytest.raises(ConversationUnavailableError):
        _ = store.hydrate_blocking(conversation_id)


async def test_the_blocking_read_retries_a_locked_database_but_not_for_long(tmp_path: Path):
    _store_, engine, _conversation_id = await _evicted_thread(tmp_path)
    calls = {"n": 0}

    def flaky(_session: Session) -> int:
        calls["n"] += 1
        if calls["n"] < 3:
            raise _locked()
        return calls["n"]

    assert db.read_blocking(engine, flaky) == 3

    calls["n"] = -100

    def always(_session: Session) -> int:
        calls["n"] += 1
        raise _locked()

    loop = asyncio.get_running_loop()
    started = loop.time()
    with pytest.raises(OperationalError):
        _ = db.read_blocking(engine, always)
    assert calls["n"] == -97
    assert loop.time() - started < 2.0


async def test_a_parked_turn_that_cannot_be_written_on_cancel_is_logged(
    caplog: pytest.LogCaptureFixture,
):
    reg = RunRegistry()

    async def parking(run: Run) -> None:
        run.park(None)

    def failing() -> None:
        raise ConversationUnavailableError("c")

    run = reg.submit(kind="chat", owner_id="operator", orchestrator=parking)
    await run.wait()
    run.on_park_cancel = failing
    caplog.set_level(logging.WARNING)

    assert await reg.cancel(run.id)

    warnings = [r for r in caplog.records if "was not written" in r.getMessage()]
    assert len(warnings) == 1
