"""A routine run parked on an approval when the app stops keeps one open `TaskRun` across
the restart, and the restored run's end finalizes it."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from pydantic import JsonValue
from sqlmodel import Session

from core.db import in_session
from core.exceptions import NotFoundError
from models._fields import utcnow
from models.task import TaskRun
from routes.deps import app_state
from services.registry import ModelRegistry
from services.scheduler import ScheduledTaskView, TaskRunResult, TaskRunSuspended

from ._helpers import await_parked, client_app, swap_tool_catalog
from ._json import json_body, obj, s
from .test_approval_routes import danger_categories
from .test_task_routes import (
    await_parked_task_run,
    await_task_run_finalized,
    create_task,
    install_sensitive_tool,
    parked_call_id,
    response_items,
)


async def _task_runs(client: httpx.AsyncClient, task_id: str) -> list[dict[str, JsonValue]]:
    return [obj(r) for r in response_items(await client.get(f"/tasks/{task_id}/runs"))]


async def _park_a_fire(client: httpx.AsyncClient, app: FastAPI) -> tuple[str, str, str, str]:
    """Fire a routine that parks; returns its task id, TaskRun id, run id and call id."""
    swap_tool_catalog(app, danger_categories())
    task_id = s((await create_task(client, pre_authorized=[]))["id"])
    fired = json_body(await client.post(f"/tasks/{task_id}/run_now"))
    task_run_id = s(fired["taskRunId"])
    run = await await_parked_task_run(app)
    for _ in range(300):
        row = (await _task_runs(client, task_id))[0]
        if row["runId"] == run.id:
            break
        await asyncio.sleep(0.01)
    else:
        raise AssertionError("the open TaskRun was never linked to its run")
    assert row["finishedAt"] is None
    return task_id, task_run_id, run.id, parked_call_id(run)


async def _decide(client: httpx.AsyncClient, run_id: str, call_id: str, *, approved: bool) -> None:
    resp = await client.post(
        f"/runs/{run_id}/approve",
        json={"decisions": [{"tool_call_id": call_id, "approved": approved}]},
    )
    assert resp.status_code == 202


async def test_an_approved_parked_routine_run_finalizes_its_one_task_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    install_sensitive_tool(monkeypatch)
    async with client_app(home=tmp_path) as (client, app):
        task_id, task_run_id, run_id, call_id = await _park_a_fire(client, app)

    async with client_app(home=tmp_path) as (client, app):
        swap_tool_catalog(app, danger_categories())
        restored = await await_parked(app, run_id)
        assert restored.kind == "task"
        assert app_state(app).scheduler.running == {}
        _ = await app_state(app).runs.parks_restored.wait()
        await asyncio.sleep(0.1)
        (open_row,) = await _task_runs(client, task_id)
        assert open_row["finishedAt"] is None and open_row["runId"] == run_id

        await _decide(client, run_id, call_id, approved=True)
        row = await await_task_run_finalized(client, task_id, task_run_id)
        assert row["outcome"] == "ok"
        assert row["runId"] == run_id
        assert row["conversationId"] == restored.conversation_id
        assert len(await _task_runs(client, task_id)) == 1


async def test_a_denied_parked_routine_run_finalizes_with_the_runs_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    install_sensitive_tool(monkeypatch)
    async with client_app(home=tmp_path) as (client, app):
        task_id, task_run_id, run_id, call_id = await _park_a_fire(client, app)

    async with client_app(home=tmp_path) as (client, app):
        swap_tool_catalog(app, danger_categories())
        _ = await await_parked(app, run_id)
        await _decide(client, run_id, call_id, approved=False)
        row = await await_task_run_finalized(client, task_id, task_run_id)
        assert row["outcome"] in {"ok", "error", "cancelled", "blocked"}
        assert row["runId"] == run_id


async def test_a_cancelled_parked_routine_run_finalizes_as_cancelled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    install_sensitive_tool(monkeypatch)
    async with client_app(home=tmp_path) as (client, app):
        task_id, task_run_id, run_id, _call_id = await _park_a_fire(client, app)

    async with client_app(home=tmp_path) as (client, app):
        swap_tool_catalog(app, danger_categories())
        _ = await await_parked(app, run_id)
        assert (await client.post(f"/runs/{run_id}/cancel")).status_code in {200, 202}
        row = await await_task_run_finalized(client, task_id, task_run_id)
        assert row["outcome"] == "cancelled"
        assert row["runId"] == run_id


async def test_a_restore_that_cannot_recompose_finalizes_the_task_run_as_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    install_sensitive_tool(monkeypatch)
    async with client_app(home=tmp_path) as (client, app):
        task_id, task_run_id, run_id, call_id = await _park_a_fire(client, app)

    async def gone(_self: ModelRegistry, _role: str, **_kwargs: object):
        raise NotFoundError("endpoint", "gone")

    async with client_app(home=tmp_path) as (client, app):
        swap_tool_catalog(app, danger_categories())
        _ = await await_parked(app, run_id)
        monkeypatch.setattr(ModelRegistry, "resolve_detailed", gone)
        resp = await client.post(
            f"/runs/{run_id}/approve",
            json={"decisions": [{"tool_call_id": call_id, "approved": True}]},
        )
        assert resp.status_code == 409
        row = await await_task_run_finalized(client, task_id, task_run_id)
        assert row["outcome"] == "blocked"
        assert "could not be resumed" in s(row["summary"])


async def test_an_open_task_run_with_no_run_is_closed_as_failed_at_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    install_sensitive_tool(monkeypatch)
    async with client_app(home=tmp_path) as (client, app):
        task_id, parked_row_id, run_id, _call_id = await _park_a_fire(client, app)
        orphan_id = "orphan-run-row"

        def add(session: Session) -> None:
            session.add(TaskRun(id=orphan_id, task_id=task_id, started_at=utcnow()))

        await in_session(app_state(app).db_engine, add)

    async with client_app(home=tmp_path) as (client, app):
        swap_tool_catalog(app, danger_categories())
        row = await await_task_run_finalized(client, task_id, orphan_id)
        assert row["outcome"] == "error"
        assert "stopped before this run finished" in s(row["summary"])
        parked = next(r for r in await _task_runs(client, task_id) if r["id"] == parked_row_id)
        assert parked["finishedAt"] is None and parked["runId"] == run_id


async def test_stopping_with_a_parked_fire_does_not_hang(monkeypatch: pytest.MonkeyPatch):
    install_sensitive_tool(monkeypatch)
    async with asyncio.timeout(10), client_app() as (client, app):
        _ = await _park_a_fire(client, app)


async def test_an_executor_that_suspends_leaves_its_task_run_open():
    async with client_app() as (client, app):
        task_id = s((await create_task(client))["id"])

        async def suspends(_view: ScheduledTaskView) -> TaskRunResult:
            raise TaskRunSuspended

        app_state(app).scheduler.executor = suspends
        fired = json_body(await client.post(f"/tasks/{task_id}/run_now"))
        task_run_id = s(fired["taskRunId"])
        for _ in range(200):
            if not app_state(app).scheduler.running:
                break
            await asyncio.sleep(0.01)
        row = next(r for r in await _task_runs(client, task_id) if r["id"] == task_run_id)
        assert row["finishedAt"] is None
