"""A parked turn is written into its thread whatever happened to the thread's cached tree
while it was parked, and a routine's row is linked before its run exists."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Literal

import pytest
from fastapi import FastAPI
from sqlalchemy import Engine
from sqlmodel import Session, col, select

import harness.turns as harness_turns
from core.db import in_session, read_blocking
from core.vault import Vault
from models.conversation import Message
from plugins.routines.models import TaskRun
from routes.deps import app_state
from services.conversations.core import StoreCore
from services.embeddings import Embedder

from ._helpers import await_parked, client_app, collect_sse_events, swap_tool_catalog
from ._json import json_body, s
from .test_approval_routes import danger_categories, install_sensitive_tool
from .test_parked_turns import park_chat
from .test_task_parked_restart import park_a_fire, task_runs
from .test_task_routes import await_task_run_finalized, create_task


async def _rows(app: FastAPI, conversation_id: str) -> list[tuple[str | None, int]]:
    def work(session: Session) -> list[tuple[str | None, int]]:
        query = (
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(col(Message.seq))
        )
        return [(m.parent_id, m.seq) for m in session.exec(query)]

    return await in_session(app_state(app).db_engine, work)


async def _thread(app: FastAPI, conversation_id: str) -> list[tuple[str, str]]:
    view = await app_state(app).conversations.messages_view(conversation_id)
    return [(t.role, t.content or "") for t in view]


def _cap_the_cache(monkeypatch: pytest.MonkeyPatch, cap: int) -> None:
    real_init = StoreCore.__init__

    def small(
        self: StoreCore,
        engine: Engine,
        vault: Vault,
        embedder: Embedder | None = None,
        *,
        max_cached_conversations: int = 64,
    ) -> None:
        _ = max_cached_conversations
        real_init(self, engine, vault, embedder, max_cached_conversations=cap)

    monkeypatch.setattr(StoreCore, "__init__", small)


def _no_errors(caplog: pytest.LogCaptureFixture) -> None:
    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR] == []


@pytest.mark.parametrize("how", ["cancel", "approve"])
async def test_a_parked_turn_is_written_into_a_thread_whose_tree_was_evicted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    how: Literal["cancel", "approve"],
):
    install_sensitive_tool(monkeypatch)
    async with client_app(home=tmp_path) as (client, app):
        swap_tool_catalog(app, danger_categories())
        run_id, call_id = await park_chat(client, app)
        run = app_state(app).runs.get(run_id)
        assert run is not None and run.conversation_id is not None
        store = app_state(app).conversations
        await store.flush()
        before = await _rows(app, run.conversation_id)
        store.evict_cache()
        if how == "cancel":
            assert (await client.post(f"/runs/{run_id}/cancel")).status_code in {200, 202}
        else:
            decision = {"tool_call_id": call_id, "approved": True}
            _ = await client.post(f"/runs/{run_id}/approve", json={"decisions": [decision]})
            _ = await collect_sse_events(client, run_id)
        await store.flush()
        after = await _rows(app, run.conversation_id)
        assert after[: len(before)] == before
        assert len(after) > len(before)
        assert [row for row in after if row[0] is None] == [(None, 0)]
        assert ("user", "delete it") in await _thread(app, run.conversation_id)
    _no_errors(caplog)


async def test_the_thread_reads_whole_after_a_restart_an_approval_and_another_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    install_sensitive_tool(monkeypatch)
    async with client_app(home=tmp_path) as (client, app):
        swap_tool_catalog(app, danger_categories())
        run_id, call_id = await park_chat(client, app)
        run = app_state(app).runs.get(run_id)
        assert run is not None and run.conversation_id is not None
        conversation_id = run.conversation_id
    async with client_app(home=tmp_path) as (client, app):
        swap_tool_catalog(app, danger_categories())
        _ = await await_parked(app, run_id)
        decision = {"tool_call_id": call_id, "approved": True}
        _ = await client.post(f"/runs/{run_id}/approve", json={"decisions": [decision]})
        _ = await collect_sse_events(client, run_id)
    async with client_app(home=tmp_path) as (_client, app):
        thread = await _thread(app, conversation_id)
        assert thread[0] == ("user", "delete it")
        assert thread[-1][0] == "assistant"
        assert [row for row in await _rows(app, conversation_id) if row[0] is None] == [(None, 0)]
    _no_errors(caplog)


async def test_more_parks_than_the_cache_holds_are_each_written_into_their_own_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    install_sensitive_tool(monkeypatch)
    async with client_app(home=tmp_path) as (client, app):
        swap_tool_catalog(app, danger_categories())
        first, first_call = await park_chat(client, app)
        second, second_call = await park_chat(client, app)
        threads = [app_state(app).runs.get(r) for r in (first, second)]
        assert all(t is not None and t.conversation_id is not None for t in threads)
        conversation_ids = [s(t.conversation_id) for t in threads if t is not None]

    _cap_the_cache(monkeypatch, 1)
    async with client_app(home=tmp_path) as (client, app):
        swap_tool_catalog(app, danger_categories())
        for run_id, call_id in ((first, first_call), (second, second_call)):
            _ = await await_parked(app, run_id)
            decision = {"tool_call_id": call_id, "approved": True}
            _ = await client.post(f"/runs/{run_id}/approve", json={"decisions": [decision]})
            _ = await collect_sse_events(client, run_id)
        await app_state(app).conversations.flush()
        for conversation_id in conversation_ids:
            assert [r for r in await _rows(app, conversation_id) if r[0] is None] == [(None, 0)]
            assert ("user", "delete it") in await _thread(app, conversation_id)
    _no_errors(caplog)


async def test_a_parked_conversation_stays_cached_while_others_come_and_go(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    install_sensitive_tool(monkeypatch)
    _cap_the_cache(monkeypatch, 2)
    async with client_app(home=tmp_path) as (client, app):
        swap_tool_catalog(app, danger_categories())
        run_id, _call_id = await park_chat(client, app)
        run = app_state(app).runs.get(run_id)
        assert run is not None and run.conversation_id is not None
        store = app_state(app).conversations
        for _ in range(5):
            _ = await store.create_conversation("operator")
        assert run.conversation_id in store.cached_ids
        assert (await client.post(f"/runs/{run_id}/cancel")).status_code in {200, 202}
        await store.flush()
        for _ in range(5):
            _ = await store.create_conversation("operator")
        assert run.conversation_id not in store.cached_ids


async def test_the_task_run_is_linked_before_the_run_is_submitted_and_unlinked_if_that_fails(
    monkeypatch: pytest.MonkeyPatch,
):
    install_sensitive_tool(monkeypatch)
    linked_first: list[bool] = []
    async with client_app() as (client, app):
        engine = app_state(app).db_engine

        def refuse(**kwargs: object) -> object:
            def read(session: Session) -> list[str | None]:
                return [r.run_id for r in session.exec(select(TaskRun))]

            linked_first.append(kwargs["run_id"] in read_blocking(engine, read))
            raise RuntimeError("submit failed")

        monkeypatch.setattr(harness_turns, "compose_turn", refuse)
        task_id = s((await create_task(client))["id"])
        fired = await client.post(f"/tasks/{task_id}/run_now")
        task_run_id = s(json_body(fired)["taskRunId"])
        row = await await_task_run_finalized(client, task_id, task_run_id)
        assert linked_first == [True]
        assert row["outcome"] == "error"
        assert row["runId"] is None
        assert (await task_runs(client, task_id))[0]["runId"] is None


async def test_a_fire_that_parks_is_still_linked(monkeypatch: pytest.MonkeyPatch):
    install_sensitive_tool(monkeypatch)
    async with client_app() as (client, app):
        task_id, _task_run_id, run_id, _call_id = await park_a_fire(client, app)
        assert (await task_runs(client, task_id))[0]["runId"] == run_id
