"""Stopping a thread stops its sub-agents, and the reports that follow wait for the operator."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from pydantic_ai import ModelResponse, TextPart
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models.function import AgentInfo, FunctionModel

from routes.deps import app_state
from runs import Run
from runs.run import StopCause
from services.agents import builtin_roster
from services.registry import ModelRegistry
from services.subagents import LaunchedSubagent, SubagentParent, SubagentUnavailableError
from tests._helpers import client_app, register_stub_provider, stub_resolution
from tests._json import arr, json_body, s
from tests.test_subagents_launch import launcher_of, records_of, settle_run

EXPLORER = builtin_roster()["explorer"]


def _hold_every_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every model request waits forever, so a spawned agent stays live until stopped."""
    gate = asyncio.Event()

    async def respond(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        _ = await gate.wait()
        return ModelResponse(parts=[TextPart(content="never")])

    async def stream(_messages: list[ModelMessage], _info: AgentInfo) -> AsyncIterator[str]:
        _ = await gate.wait()
        yield "never"

    async def resolve(self: ModelRegistry, _role: str, **_kwargs: object):
        return await stub_resolution(self, FunctionModel(respond, stream_function=stream))

    register_stub_provider(monkeypatch)
    monkeypatch.setattr(ModelRegistry, "resolve_detailed", resolve)


async def _spawn(app: FastAPI, handle: str, parent: str) -> LaunchedSubagent:
    started = await launcher_of(app).launch(
        "operator",
        EXPLORER,
        "look into the parser",
        handle=handle,
        parent=SubagentParent(conversation_id=parent),
    )
    await asyncio.sleep(0.05)
    return started


async def _thread(app: FastAPI) -> str:
    return await app_state(app).conversations.create_conversation("operator", title="root")


def _run(app: FastAPI, run_id: str) -> Run:
    run = app_state(app).runs.get(run_id)
    assert run is not None
    return run


def _hold_run(app: FastAPI, conversation_id: str) -> Run:
    """A run of the thread's own that stays live until cancelled."""
    gate = asyncio.Event()

    async def hold(_run: Run) -> None:
        _ = await gate.wait()

    return app_state(app).runs.submit(
        kind="probe", owner_id="operator", orchestrator=hold, conversation_id=conversation_id
    )


async def _stopped_cleanly(app: FastAPI, root: str, child: LaunchedSubagent) -> None:
    await settle_run(app, child.run_id)
    run = _run(app, child.run_id)
    assert run.stop_cause is StopCause.operator
    row = await records_of(app).by_child(child.conversation_id)
    assert row is not None
    assert row.status == "cancelled"

    inbox = app_state(app).runs.inbox(root, "operator")
    assert inbox.waiting_for_operator
    [report] = inbox.messages
    assert "cancelled (stopped by the operator)" in report.text
    assert "Don't relaunch it" in report.text
    # Nothing woke the thread: the only run it ever had is the probe, if any.
    assert [r.kind for r in app_state(app).runs.list() if r.conversation_id == root] in (
        [],
        ["probe"],
    )


async def test_stopping_a_running_thread_stops_its_agents_and_holds_their_reports(
    monkeypatch: pytest.MonkeyPatch,
):
    _hold_every_model(monkeypatch)
    async with client_app() as (client, app):
        root = await _thread(app)
        child = await _spawn(app, "reader", root)
        own = _hold_run(app, root)

        resp = await client.post(f"/runs/{own.id}/cancel", json={"hold_inbox": True})

        assert resp.status_code == 202
        assert own.stop_cause is StopCause.operator
        await _stopped_cleanly(app, root, child)


async def test_stopping_an_idle_thread_stops_the_agents_it_still_has(
    monkeypatch: pytest.MonkeyPatch,
):
    _hold_every_model(monkeypatch)
    async with client_app() as (client, app):
        root = await _thread(app)
        child = await _spawn(app, "reader", root)

        resp = await client.post(f"/conversations/{root}/agents/stop")

        assert resp.status_code == 200
        assert json_body(resp)["stopped"] == ["reader"]
        await _stopped_cleanly(app, root, child)


async def test_the_stop_reaches_agents_below_an_agent(monkeypatch: pytest.MonkeyPatch):
    _hold_every_model(monkeypatch)
    async with client_app() as (client, app):
        root = await _thread(app)
        lead = await app_state(app).agent_store.create(
            "operator",
            {
                "name": "lead",
                "description": "Splits work up.",
                "instructions": "Delegate.",
                "can_delegate": True,
                "permission_ceiling": "plan",
            },
            published=True,
        )
        planner = await launcher_of(app).launch(
            "operator",
            lead,
            "split the parser work up",
            handle="planner",
            parent=SubagentParent(conversation_id=root),
        )
        reader = await _spawn(app, "reader", planner.conversation_id)

        resp = await client.post(f"/conversations/{root}/agents/stop")

        assert sorted(s(name) for name in arr(json_body(resp)["stopped"])) == ["planner", "reader"]
        for started in (planner, reader):
            await settle_run(app, started.run_id)
            row = await records_of(app).by_child(started.conversation_id)
            assert row is not None
            assert row.status == "cancelled"
            assert _run(app, started.run_id).stop_cause is StopCause.operator


async def test_stopping_an_idle_thread_with_nothing_live_changes_nothing(
    monkeypatch: pytest.MonkeyPatch,
):
    _hold_every_model(monkeypatch)
    async with client_app() as (client, app):
        root = await _thread(app)

        resp = await client.post(f"/conversations/{root}/agents/stop")

        assert resp.status_code == 200
        assert json_body(resp)["stopped"] == []
        assert not app_state(app).runs.inbox(root, "operator").waiting_for_operator


async def test_an_unknown_thread_is_a_404(monkeypatch: pytest.MonkeyPatch):
    _hold_every_model(monkeypatch)
    async with client_app() as (client, _app):
        assert (await client.post("/conversations/nope/agents/stop")).status_code == 404


async def test_an_agent_stopped_by_its_parent_says_so(monkeypatch: pytest.MonkeyPatch):
    _hold_every_model(monkeypatch)
    async with client_app() as (_client, app):
        root = await _thread(app)
        child = await _spawn(app, "reader", root)

        _ = await launcher_of(app).stop("operator", root, "reader")
        await settle_run(app, child.run_id)

        assert _run(app, child.run_id).stop_cause is StopCause.parent
        with pytest.raises(SubagentUnavailableError, match="stopped by its parent"):
            _ = await launcher_of(app).message("operator", root, "reader", "still there?")


async def test_a_message_to_an_agent_the_operator_stopped_names_the_operator(
    monkeypatch: pytest.MonkeyPatch,
):
    _hold_every_model(monkeypatch)
    async with client_app() as (client, app):
        root = await _thread(app)
        child = await _spawn(app, "reader", root)
        _ = await client.post(f"/conversations/{root}/agents/stop")
        await settle_run(app, child.run_id)

        with pytest.raises(SubagentUnavailableError, match="stopped by the operator") as refused:
            _ = await launcher_of(app).message("operator", root, "reader", "still there?")

        assert "unless the operator asks" in str(refused.value)
