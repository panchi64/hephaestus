"""A run parked on the operator survives a restart: stored at the park, restored after
unlock, resumed by an approval, and deleted whenever the park ends."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import override

import httpx
import pytest
from fastapi import FastAPI
from pydantic_ai import (
    DeferredToolRequests,
    ModelRequest,
    ToolApproved,
    ToolCallPart,
    ToolDenied,
    UserPromptPart,
)
from pydantic_ai.models.test import TestModel
from sqlmodel import Session, select

from agent import ParkedTurn, build_chat_orchestrator, build_resume_orchestrator
from agent.gating import GrantApproved
from agent.parked_codec import decode_park, encode_park
from core.db import in_session
from core.exceptions import NotFoundError
from models.parked_turn import ParkedTurnRecord
from routes.deps import app_state
from runs import Endpoint, Event, ParkedJournal, Run, RunRegistry, RunStatus, RunStream
from services.agents import builtin_roster
from services.conversations import ConversationBinding
from services.registry import ModelRegistry
from services.subagents import SubagentLauncher

from ._helpers import (
    await_parked,
    client_app,
    collect_sse_events,
    parked_turn,
    run_bodies,
    swap_tool_catalog,
)
from ._json import json_body, s
from .test_approval_routes import danger_categories, install_sensitive_tool


async def _stored(app: FastAPI) -> list[ParkedTurnRecord]:
    def work(session: Session) -> list[ParkedTurnRecord]:
        return list(session.exec(select(ParkedTurnRecord)))

    return await in_session(app_state(app).db_engine, work)


async def _first(events: AsyncIterator[Event]) -> Event:
    async for event in events:
        return event
    raise AssertionError("the stream ended without an event")


async def _await_row(app: FastAPI, run_id: str, *, present: bool = True) -> None:
    for _ in range(300):
        if any(row.run_id == run_id for row in await _stored(app)) == present:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"row for {run_id} never {'appeared' if present else 'went'}")


async def _park_chat(client: httpx.AsyncClient, app: FastAPI) -> tuple[str, str]:
    run_id = s(json_body(await client.post("/chat", json={"prompt": "delete it"}))["run_id"])
    run = await await_parked(app, run_id)
    await _await_row(app, run_id)
    return run_id, parked_turn(run).requests.approvals[0].tool_call_id


async def test_a_clean_restart_restores_a_parked_chat_run_and_approving_finishes_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    install_sensitive_tool(monkeypatch)
    async with client_app(home=tmp_path) as (client, app):
        swap_tool_catalog(app, danger_categories())
        run_id, call_id = await _park_chat(client, app)
        before = app_state(app).runs.get(run_id)
        assert before is not None
        last_seq = before.stream.last_seq

    # The shutdown neither cancelled the run nor told its stream it ended.
    assert before.status is RunStatus.awaiting_input
    assert not before.stream.closed
    assert "run.ended" not in [b.type for b in run_bodies(before)]

    async with client_app(home=tmp_path) as (client, app):
        swap_tool_catalog(app, danger_categories())
        restored = await await_parked(app, run_id)
        assert restored is not before
        assert restored.kind == "chat" and restored.conversation_id == before.conversation_id
        assert restored.stream.last_seq > last_seq
        assert parked_turn(restored).agent is None

        resp = await client.post(
            f"/runs/{run_id}/approve",
            json={"decisions": [{"tool_call_id": call_id, "approved": True}]},
        )
        assert resp.status_code == 202
        types = [e["type"] for e in await collect_sse_events(client, run_id)]

        assert "tool.completed" in types
        assert types[-1] == "run.ended"
        assert restored.status is RunStatus.done
        await _await_row(app, run_id, present=False)


async def test_a_crash_leaves_the_park_to_restore(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    install_sensitive_tool(monkeypatch)

    async def no_shutdown(_self: RunRegistry, _timeout_s: float = 5.0) -> None:
        pass

    with monkeypatch.context() as crash:
        crash.setattr(RunRegistry, "shutdown", no_shutdown)
        async with client_app(home=tmp_path) as (client, app):
            swap_tool_catalog(app, danger_categories())
            run_id, call_id = await _park_chat(client, app)

    async with client_app(home=tmp_path) as (client, app):
        swap_tool_catalog(app, danger_categories())
        _ = await await_parked(app, run_id)
        resp = await client.post(
            f"/runs/{run_id}/approve",
            json={"decisions": [{"tool_call_id": call_id, "approved": True}]},
        )
        assert resp.status_code == 202
        types = [e["type"] for e in await collect_sse_events(client, run_id)]
        assert types[-1] == "run.ended"


async def test_a_reconnect_with_an_old_last_event_id_receives_the_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    install_sensitive_tool(monkeypatch)
    async with client_app(home=tmp_path) as (client, app):
        swap_tool_catalog(app, danger_categories())
        run_id, call_id = await _park_chat(client, app)
        old = app_state(app).runs.get(run_id)
        assert old is not None
        approval_seq = next(
            e.seq for e in old.stream.replay() if e.body.type == "approval.required"
        )

    async with client_app(home=tmp_path) as (client, app):
        swap_tool_catalog(app, danger_categories())
        restored = await await_parked(app, run_id)

        event = await asyncio.wait_for(
            _first(restored.stream.subscribe(after_seq=approval_seq - 1)), timeout=5
        )
        assert event.body.type == "approval.required"
        assert event.seq > approval_seq

        resp = await client.post(
            f"/runs/{run_id}/approve",
            json={"decisions": [{"tool_call_id": call_id, "approved": True}]},
        )
        assert resp.status_code == 202
        events = await collect_sse_events(client, run_id)
        assert "approval.required" in [e["type"] for e in events]


async def test_a_missing_model_blocks_the_restored_run_and_denies_its_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    install_sensitive_tool(monkeypatch)
    async with client_app(home=tmp_path) as (client, app):
        swap_tool_catalog(app, danger_categories())
        run_id, call_id = await _park_chat(client, app)

    async def gone(_self: ModelRegistry, _role: str, **_kwargs: object):
        raise NotFoundError("endpoint", "gone")

    async with client_app(home=tmp_path) as (client, app):
        swap_tool_catalog(app, danger_categories())
        restored = await await_parked(app, run_id)
        monkeypatch.setattr(ModelRegistry, "resolve_detailed", gone)

        resp = await client.post(
            f"/runs/{run_id}/approve",
            json={"decisions": [{"tool_call_id": call_id, "approved": True}]},
        )
        assert resp.status_code == 409
        assert "pending calls were denied" in s(json_body(resp)["detail"])
        assert restored.status is RunStatus.blocked
        types = [e["type"] for e in await collect_sse_events(client, run_id)]
        assert "tool.completed" not in types
        assert types[-1] == "run.ended"
        await _await_row(app, run_id, present=False)


async def test_a_parked_subagent_restores_and_finishes_after_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    install_sensitive_tool(monkeypatch)
    worker = builtin_roster()["worker"]
    async with client_app(home=tmp_path) as (_client, app):
        swap_tool_catalog(app, danger_categories())
        launcher = app_state(app).capabilities.get_optional(SubagentLauncher)
        assert launcher is not None
        started = await launcher.launch("operator", worker, "delete it", handle="deleter")
        run = await await_parked(app, started.run_id)
        await _await_row(app, started.run_id)
        call_id = parked_turn(run).requests.approvals[0].tool_call_id
        assert run.kind == "linked"

    async with client_app(home=tmp_path) as (client, app):
        swap_tool_catalog(app, danger_categories())
        restored = await await_parked(app, started.run_id)
        resp = await client.post(
            f"/runs/{started.run_id}/approve",
            json={"decisions": [{"tool_call_id": call_id, "approved": True}]},
        )
        assert resp.status_code == 202
        types = [e["type"] for e in await collect_sse_events(client, started.run_id)]
        assert "tool.completed" in types
        assert restored.status is RunStatus.done


async def test_cancelling_a_parked_run_deletes_its_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    install_sensitive_tool(monkeypatch)
    async with client_app(home=tmp_path) as (client, app):
        swap_tool_catalog(app, danger_categories())
        run_id, _ = await _park_chat(client, app)
        assert (await client.post(f"/runs/{run_id}/cancel")).status_code == 202
        await _await_row(app, run_id, present=False)


class _Recording(ParkedJournal):
    def __init__(self) -> None:
        self.recorded: list[str] = []
        self.forgotten: list[str] = []

    @override
    def record(self, run: Run) -> None:
        self.recorded.append(run.id)

    @override
    def forget(self, run_id: str) -> None:
        self.forgotten.append(run_id)


def _park_orchestrator():
    return build_chat_orchestrator(
        "delete the thing",
        model=TestModel(custom_output_text="done"),
        categories=danger_categories(),
    )


async def test_the_registry_writes_a_park_once_and_forgets_it_when_it_resumes():
    journal = _Recording()
    reg = RunRegistry()
    reg.attach_parked_journal(journal)
    run = reg.submit(kind="chat", owner_id="operator", orchestrator=_park_orchestrator())
    await run.wait()
    assert journal.recorded == [run.id] and journal.forgotten == []

    parked = parked_turn(run)
    call_id = parked.requests.approvals[0].tool_call_id
    _ = await reg.resume(run.id, build_resume_orchestrator(parked, {call_id: ToolApproved()}))
    await run.wait()

    assert run.status is RunStatus.done
    assert journal.forgotten == [run.id]


async def test_a_run_that_never_parked_costs_no_delete():
    journal = _Recording()
    reg = RunRegistry()
    reg.attach_parked_journal(journal)
    run = reg.submit(
        kind="chat",
        owner_id="operator",
        orchestrator=build_chat_orchestrator(
            "hi", model=TestModel(custom_output_text="ok", call_tools=[])
        ),
    )
    await run.wait()
    assert run.status is RunStatus.done
    assert journal.recorded == [] and journal.forgotten == []


async def test_cancel_and_block_forget_a_park():
    journal = _Recording()
    reg = RunRegistry()
    reg.attach_parked_journal(journal)
    cancelled = reg.submit(kind="chat", owner_id="operator", orchestrator=_park_orchestrator())
    blocked = reg.submit(kind="chat", owner_id="operator", orchestrator=_park_orchestrator())
    await cancelled.wait()
    await blocked.wait()

    assert await reg.cancel(cancelled.id)
    assert await reg.block_parked(blocked.id, "gone")
    assert not await reg.block_parked(blocked.id, "again")
    assert sorted(journal.forgotten) == sorted([cancelled.id, blocked.id])
    assert blocked.status is RunStatus.blocked and blocked.stream.closed


async def test_shutdown_leaves_a_parked_run_alone_and_cancels_the_rest():
    journal = _Recording()
    reg = RunRegistry()
    reg.attach_parked_journal(journal)
    run = reg.submit(kind="chat", owner_id="operator", orchestrator=_park_orchestrator())
    await run.wait()
    gate = asyncio.Event()

    async def hold(_run: Run) -> None:
        _ = await gate.wait()

    running = reg.submit(kind="chat", owner_id="operator", orchestrator=hold)
    await asyncio.sleep(0)

    await reg.shutdown()

    assert run.status is RunStatus.awaiting_input and not run.stream.closed
    assert "run.ended" not in [b.type for b in run_bodies(run)]
    assert journal.forgotten == []
    assert running.status is RunStatus.cancelled


def test_a_park_round_trips_through_its_json():
    call = ToolCallPart("danger_delete_thing", {"name": "x"}, tool_call_id="c1")
    other = ToolCallPart("danger_other", {"n": 1}, tool_call_id="c2")
    binding = ConversationBinding(project_id="p1", worktree=True, permission="edit", spawned=True)
    parked = ParkedTurn(
        agent=None,
        message_history=[ModelRequest(parts=[UserPromptPart("delete it")])],
        requests=DeferredToolRequests(
            approvals=[call, other], metadata={"c1": {"explanation": "e"}}
        ),
        announced={"c1"},
        settled={
            "c2": GrantApproved(needs_whole_tool=True),
            "c3": ToolDenied(message="no"),
            "c4": ToolApproved(override_args={"k": "v"}),
        },
        conversation_id="conv",
        persist_from=3,
        persist_from_parts=1,
        clean_drop=(4, 5),
        prompt_message_id="m1",
        binding=binding,
        vision=False,
        workspace_key="ws",
        endpoint_id="e1",
        model="m",
        titled=True,
    )
    run = Run(
        id="r",
        kind="chat",
        owner_id="operator",
        stream=RunStream(),
        endpoint=Endpoint("e1", "local", 2),
    )

    back, endpoint = decode_park(encode_park(parked, run))

    assert back.agent is None and back.titled and back.conversation_id == "conv"
    assert back.message_history == parked.message_history
    assert back.requests.approvals == parked.requests.approvals
    assert back.requests.metadata == {"c1": {"explanation": "e"}}
    assert back.binding == binding
    assert back.announced == {"c1"} and back.clean_drop == (4, 5)
    assert (back.persist_from, back.persist_from_parts, back.prompt_message_id) == (3, 1, "m1")
    assert (back.endpoint_id, back.model, back.workspace_key, back.vision) == (
        "e1",
        "m",
        "ws",
        False,
    )
    grant = back.settled["c2"]
    assert isinstance(grant, GrantApproved) and grant.needs_whole_tool
    assert back.settled["c3"] == ToolDenied(message="no")
    assert back.settled["c4"] == ToolApproved(override_args={"k": "v"})
    assert endpoint == Endpoint("e1", "local", 2)
