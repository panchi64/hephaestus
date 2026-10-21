"""What a restore refuses to resume, and how an unreadable or unresumable park ends."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import cast

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from pydantic_ai import (
    DeferredToolRequests,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from sqlmodel import Session, select

import agent.finalize as finalize_module
import routes.runs as runs_routes
from agent import build_resume_orchestrator
from agent.finalize import persist_parked_cancel
from agent.parking import ParkedTurn
from core.db import in_session
from models.parked_turn import ParkedTurnRecord
from models.task import ScheduledTask
from routes.chat.turn import resolve_turn_models
from routes.deps import app_state
from runs import Run, RunRegistry, RunStatus, RunStream
from runs.events import AnswerDelta
from services.agents import builtin_roster
from services.subagents import SubagentLauncher, SubagentParent

from ._helpers import await_parked, client_app, collect_sse_events, parked_turn, swap_tool_catalog
from ._json import json_body, s
from .test_approval_routes import danger_categories, install_sensitive_tool
from .test_parked_turns import await_row, park_chat, stored_parks
from .test_registry import make_registry
from .test_task_parked_restart import park_a_fire, task_runs
from .test_task_routes import await_task_run_finalized
from .test_tool_policy import spy_disabled_tools


async def _approve(client: httpx.AsyncClient, run_id: str, call_id: str) -> httpx.Response:
    return await client.post(
        f"/runs/{run_id}/approve",
        json={"decisions": [{"tool_call_id": call_id, "approved": True}]},
    )


async def _no_shutdown(_self: RunRegistry, _timeout_s: float = 5.0) -> None:
    pass


async def _update_rows(app: FastAPI, **values: str) -> None:
    def work(session: Session) -> None:
        for row in session.exec(select(ParkedTurnRecord)):
            for name, value in values.items():
                setattr(row, name, value)
            session.add(row)

    await in_session(app_state(app).db_engine, work)


async def test_a_chat_park_whose_agent_is_gone_is_blocked_not_resumed_as_the_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    install_sensitive_tool(monkeypatch)
    async with client_app(home=tmp_path) as (client, app):
        swap_tool_catalog(app, danger_categories())
        run_id, call_id = await park_chat(client, app)
        conversation_id = app_state(app).runs.get(run_id)
        assert conversation_id is not None and conversation_id.conversation_id is not None
        await app_state(app).conversations.set_agent_name(conversation_id.conversation_id, "ghost")

    async with client_app(home=tmp_path) as (client, app):
        swap_tool_catalog(app, danger_categories())
        restored = await await_parked(app, run_id)
        resp = await _approve(client, run_id, call_id)
        assert resp.status_code == 409
        assert "is gone" in s(json_body(resp)["detail"])
        assert restored.status is RunStatus.blocked


async def test_a_routine_park_whose_agent_is_gone_is_blocked_and_its_task_run_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    install_sensitive_tool(monkeypatch)
    async with client_app(home=tmp_path) as (client, app):
        task_id, task_run_id, run_id, call_id = await park_a_fire(client, app)
        run = app_state(app).runs.get(run_id)
        assert run is not None and run.conversation_id is not None
        await app_state(app).conversations.set_agent_name(run.conversation_id, "ghost")

    async with client_app(home=tmp_path) as (client, app):
        swap_tool_catalog(app, danger_categories())
        _ = await await_parked(app, run_id)
        assert (await _approve(client, run_id, call_id)).status_code == 409
        row = await await_task_run_finalized(client, task_id, task_run_id)
        assert row["outcome"] == "blocked"


async def test_a_routine_park_whose_routine_is_gone_is_blocked_at_restore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    install_sensitive_tool(monkeypatch)
    async with client_app(home=tmp_path) as (client, app):
        task_id, _task_run_id, run_id, _call_id = await park_a_fire(client, app)

        def drop(session: Session) -> None:
            session.delete(session.get(ScheduledTask, task_id))

        await in_session(app_state(app).db_engine, drop)

    async with client_app(home=tmp_path) as (client, app):
        restored = None
        for _ in range(300):
            restored = app_state(app).runs.get(run_id)
            if restored is not None and restored.is_terminal:
                break
            await asyncio.sleep(0.01)
        assert restored is not None and restored.status is RunStatus.blocked
        assert "no longer exists" in (restored.detail or "")
        await await_row(app, run_id, present=False)
        items, _ = await app_state(app).notifications.list_notifications("operator", limit=50)
        assert [n.kind for n in items].count("run_restore_failed") == 1


async def test_a_routine_deleting_itself_is_not_cancelled_by_the_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    install_sensitive_tool(monkeypatch)
    async with client_app(home=tmp_path) as (client, app):
        task_id, _task_run_id, run_id, _call_id = await park_a_fire(client, app)
        await app_state(app).routines.delete("operator", task_id, by_run=run_id)
        run = app_state(app).runs.get(run_id)
        assert run is not None and run.status is RunStatus.awaiting_input


async def test_the_readable_parks_are_registered_before_an_unreadable_one_ends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    install_sensitive_tool(monkeypatch)
    worker = builtin_roster()["worker"]
    with monkeypatch.context() as crash:
        crash.setattr(RunRegistry, "shutdown", _no_shutdown)
        async with client_app(home=tmp_path) as (client, app):
            swap_tool_catalog(app, danger_categories())
            parent_id, _call_id = await park_chat(client, app)
            launcher = app_state(app).capabilities.get_optional(SubagentLauncher)
            assert launcher is not None
            parent_run = app_state(app).runs.get(parent_id)
            assert parent_run is not None
            child = await launcher.launch(
                "operator",
                worker,
                "delete it",
                handle="deleter",
                parent=SubagentParent(conversation_id=parent_run.conversation_id),
            )
            _ = await await_parked(app, child.run_id)
            await await_row(app, child.run_id)

            def corrupt(session: Session) -> None:
                row = session.get(ParkedTurnRecord, child.run_id)
                assert row is not None
                row.payload_enc = "not a sealed payload"
                session.add(row)

            await in_session(app_state(app).db_engine, corrupt)

    seen: list[bool] = []
    real = RunRegistry.end_unrestorable

    def spy(self: RunRegistry, run: Run, detail: str) -> None:
        seen.append(self.get(parent_id) is not None)
        real(self, run, detail)

    monkeypatch.setattr(RunRegistry, "end_unrestorable", spy)
    async with client_app(home=tmp_path) as (client, app):
        parent = await await_parked(app, parent_id)
        for _ in range(300):
            if seen:
                break
            await asyncio.sleep(0.01)
        assert seen == [True]
        for _ in range(300):
            if parent.inbox is not None and parent.inbox.messages:
                break
            await asyncio.sleep(0.01)
        assert parent.inbox is not None and parent.inbox.messages
        assert parent.status is RunStatus.awaiting_input
        assert app_state(app).runs.get(parent_id) is parent


async def test_a_cancelled_park_leaves_no_tool_call_without_a_return(
    monkeypatch: pytest.MonkeyPatch,
):
    done = ToolCallPart("ran", {}, tool_call_id="done")
    deferred = ToolCallPart("danger_delete_thing", {}, tool_call_id="deferred")
    parked = ParkedTurn(
        agent=None,
        message_history=[
            ModelRequest(parts=[UserPromptPart("go")]),
            ModelResponse(parts=[done, deferred]),
            ModelRequest(parts=[ToolReturnPart("ran", "ok", tool_call_id="done")]),
        ],
        requests=DeferredToolRequests(approvals=[deferred]),
        conversation_id="c",
    )
    run = Run(id="r", kind="chat", owner_id="operator", stream=RunStream())
    run.park(parked)
    run.status = RunStatus.cancelled
    captured: list[list[ModelMessage]] = []

    def spy(_run: Run, _store: object):
        def record(history: list[ModelMessage], _detail: str, _context: object) -> None:
            captured.append(history)

        return record

    monkeypatch.setattr(finalize_module, "flush_recorder", spy)
    persist_parked_cancel(run, store=None)

    [history] = captured
    calls = {
        p.tool_call_id
        for m in history
        if isinstance(m, ModelResponse)
        for p in m.parts
        if isinstance(p, ToolCallPart)
    }
    returns = {
        p.tool_call_id
        for m in history
        if isinstance(m, ModelRequest)
        for p in m.parts
        if isinstance(p, ToolReturnPart)
    }
    assert calls == returns == {"done", "deferred"}


async def test_an_unknown_model_on_an_existing_endpoint_cannot_recompose():
    registry = await make_registry()
    endpoint = await registry.create_endpoint("operator", name="p", base_url="http://p/v1")
    with pytest.raises(HTTPException):
        _ = await resolve_turn_models(registry, endpoint.id, "no-such-model")


async def test_a_park_beside_a_live_run_is_blocked_and_the_thread_stays_usable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    install_sensitive_tool(monkeypatch)
    with monkeypatch.context() as crash:
        crash.setattr(RunRegistry, "shutdown", _no_shutdown)
        async with client_app(home=tmp_path) as (client, app):
            swap_tool_catalog(app, danger_categories())
            run_id, _call_id = await park_chat(client, app)
            original = app_state(app).runs.get(run_id)
            assert original is not None
            conversation_id = original.conversation_id
            assert conversation_id is not None

    real = RunRegistry.active_run_for
    busy = {"on": True}

    def occupied(self: RunRegistry, conversation: str, owner: str) -> Run | None:
        if busy["on"]:
            return Run(id="other", kind="chat", owner_id=owner, stream=RunStream())
        return real(self, conversation, owner)

    monkeypatch.setattr(RunRegistry, "active_run_for", occupied)
    async with client_app(home=tmp_path) as (client, app):
        swap_tool_catalog(app, danger_categories())
        blocked = None
        for _ in range(300):
            blocked = app_state(app).runs.get(run_id)
            if blocked is not None and blocked.is_terminal:
                break
            await asyncio.sleep(0.01)
        assert blocked is not None and blocked.status is RunStatus.blocked
        busy["on"] = False
        before = await app_state(app).conversations.messages_view(conversation_id)
        sent = await client.post(
            "/chat", json={"prompt": "again", "conversation_id": conversation_id}
        )
        assert sent.status_code == 202
        _ = await await_parked(app, s(json_body(sent)["run_id"]))
        after = await app_state(app).conversations.messages_view(conversation_id)
        assert len(after) > len(before)
        assert [t.content for t in after if t.role == "user"].count("delete it") == 1


async def test_deleting_a_routine_cancels_its_parked_run_and_drops_the_stored_park(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    install_sensitive_tool(monkeypatch)
    async with client_app(home=tmp_path) as (client, app):
        task_id, _task_run_id, run_id, _call_id = await park_a_fire(client, app)
        await await_row(app, run_id)
        run = app_state(app).runs.get(run_id)
        assert run is not None
        assert (await client.delete(f"/tasks/{task_id}")).status_code == 204
        assert run.status is RunStatus.cancelled
        await await_row(app, run_id, present=False)


async def test_deleting_a_routine_after_a_restart_cancels_the_restored_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    install_sensitive_tool(monkeypatch)
    async with client_app(home=tmp_path) as (client, app):
        task_id, _task_run_id, run_id, _call_id = await park_a_fire(client, app)

    async with client_app(home=tmp_path) as (client, app):
        swap_tool_catalog(app, danger_categories())
        restored = await await_parked(app, run_id)
        assert (await client.delete(f"/tasks/{task_id}")).status_code == 204
        assert restored.status is RunStatus.cancelled
        await await_row(app, run_id, present=False)
        assert (await client.get(f"/tasks/{task_id}/runs")).status_code == 404


async def test_deleting_a_routine_cancels_its_run_that_is_still_running(
    monkeypatch: pytest.MonkeyPatch,
):
    install_sensitive_tool(monkeypatch)
    gate = asyncio.Event()

    async def hold(self: RunRegistry, run_id: str) -> bool:
        gate.set()
        return await real_cancel(self, run_id)

    real_cancel = RunRegistry.cancel
    monkeypatch.setattr(RunRegistry, "cancel", hold)
    async with client_app() as (client, app):
        swap_tool_catalog(app, danger_categories())
        task_id, _task_run_id, run_id, _call_id = await park_a_fire(client, app)
        assert (await client.delete(f"/tasks/{task_id}")).status_code == 204
        assert gate.is_set()
        run = app_state(app).runs.get(run_id)
        assert run is not None and run.status is RunStatus.cancelled


async def test_an_unreadable_park_is_dropped_blocked_and_announced_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    install_sensitive_tool(monkeypatch)
    with monkeypatch.context() as crash:
        crash.setattr(RunRegistry, "shutdown", _no_shutdown)
        async with client_app(home=tmp_path) as (client, app):
            swap_tool_catalog(app, danger_categories())
            run_id, _call_id = await park_chat(client, app)
            await _update_rows(app, payload_enc="not a sealed payload")

    async with client_app(home=tmp_path) as (client, app):
        gone = app_state(app).runs.get(run_id)
        for _ in range(300):
            gone = app_state(app).runs.get(run_id)
            if gone is not None and gone.is_terminal:
                break
            await asyncio.sleep(0.01)
        assert gone is not None and gone.status is RunStatus.blocked
        await await_row(app, run_id, present=False)
        items, _ = await app_state(app).notifications.list_notifications("operator", limit=50)
        assert [n.kind for n in items].count("run_restore_failed") == 1

    async with client_app(home=tmp_path) as (client, app):
        await asyncio.sleep(0.2)
        assert app_state(app).runs.get(run_id) is None
        assert await stored_parks(app) == []


async def test_a_park_sealed_for_another_kind_is_an_undecodable_park(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    install_sensitive_tool(monkeypatch)
    with monkeypatch.context() as crash:
        crash.setattr(RunRegistry, "shutdown", _no_shutdown)
        async with client_app(home=tmp_path) as (client, app):
            swap_tool_catalog(app, danger_categories())
            run_id, _call_id = await park_chat(client, app)
            await _update_rows(app, kind="subagent")

    async with client_app(home=tmp_path) as (client, app):
        for _ in range(300):
            gone = app_state(app).runs.get(run_id)
            if gone is not None and gone.is_terminal:
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("the mismatched park was never ended")
        assert gone.status is RunStatus.blocked
        await await_row(app, run_id, present=False)


async def test_a_task_run_of_an_unreadable_park_is_finalized_as_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    install_sensitive_tool(monkeypatch)
    with monkeypatch.context() as crash:
        crash.setattr(RunRegistry, "shutdown", _no_shutdown)
        async with client_app(home=tmp_path) as (client, app):
            task_id, task_run_id, _run_id, _call_id = await park_a_fire(client, app)
            await _update_rows(app, payload_enc="not a sealed payload")

    async with client_app(home=tmp_path) as (client, app):
        row = await await_task_run_finalized(client, task_id, task_run_id, timeout=10)
        assert row["outcome"] == "blocked"
        assert len(await task_runs(client, task_id)) == 1


async def test_replayed_approvals_sit_above_events_emitted_while_parked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    install_sensitive_tool(monkeypatch)
    async with client_app(home=tmp_path) as (client, app):
        swap_tool_catalog(app, danger_categories())
        run_id, _call_id = await park_chat(client, app)
        parked = app_state(app).runs.get(run_id)
        assert parked is not None
        _ = parked.emit(AnswerDelta(text="late"))
        seen = parked.stream.last_seq

    async with client_app(home=tmp_path) as (client, app):
        restored = await await_parked(app, run_id)
        approvals = [e.seq for e in restored.stream.replay() if e.body.type == "approval.required"]
        assert approvals and min(approvals) > seen


async def test_a_blocked_park_is_stored_with_a_denial_for_every_pending_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    install_sensitive_tool(monkeypatch)
    captured: list[list[object]] = []

    def spy(_run: Run, _store: object):
        def record(history: list[object], _detail: str, _context: object) -> None:
            captured.append(history)

        return record

    monkeypatch.setattr(finalize_module, "flush_recorder", spy)
    async with client_app(home=tmp_path) as (client, app):
        swap_tool_catalog(app, danger_categories())
        run_id, call_id = await park_chat(client, app)
        run = app_state(app).runs.get(run_id)
        assert run is not None
        parked = parked_turn(run)
        run.block("gone")
        persist_parked_cancel(run, store=None)

    [history] = captured
    last = cast(ModelRequest, history[-1])
    returns = [p for p in last.parts if isinstance(p, ToolReturnPart)]
    assert [p.tool_call_id for p in returns] == [a.tool_call_id for a in parked.requests.approvals]
    assert call_id in [p.tool_call_id for p in returns]
    assert all(p.outcome == "denied" for p in returns)


async def test_a_sub_agent_that_may_not_delegate_gets_no_spawn_tool_after_an_approval(
    monkeypatch: pytest.MonkeyPatch,
):
    install_sensitive_tool(monkeypatch)
    worker = builtin_roster()["worker"]
    captured: dict[str, frozenset[str]] = {}
    monkeypatch.setattr(
        runs_routes,
        "build_resume_orchestrator",
        spy_disabled_tools(build_resume_orchestrator, captured),
    )
    async with client_app() as (client, app):
        swap_tool_catalog(app, danger_categories())
        launcher = app_state(app).capabilities.get_optional(SubagentLauncher)
        assert launcher is not None
        started = await launcher.launch("operator", worker, "delete it", handle="deleter")
        run = await await_parked(app, started.run_id)
        call_id = parked_turn(run).requests.approvals[0].tool_call_id
        assert (await _approve(client, started.run_id, call_id)).status_code == 202
        _ = await collect_sse_events(client, started.run_id)

    assert "agents_spawn" in captured["disabled"]
