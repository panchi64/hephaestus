"""What an unattended turn withholds, fresh and after a restart: the agent's own withheld
tools must reach every kind's disabled set, and a restored park must carry the same one."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi import FastAPI
from sqlmodel import Session, select

from agent.recompose import recompose
from core.config import get_settings
from core.db import in_session
from models.parked_turn import ParkedTurnRecord
from routes.chat.thread_composer import ThreadComposer
from routes.deps import app_state
from services.agents import builtin_roster
from services.agents.thread import DELEGATION_TOOLS, as_thread
from services.conversations import ConversationBinding
from services.subagents import SubagentLauncher

from ._helpers import await_parked, client_app, parked_turn, swap_tool_catalog
from ._json import json_body, s
from .test_approval_routes import danger_categories, install_sensitive_tool


def _composer(app: FastAPI) -> ThreadComposer:
    state = app_state(app)
    return ThreadComposer(
        agents=state.agents,
        model_registry=state.models,
        conversations=state.conversations,
        settings_store=state.settings_store,
        offline=state.offline,
        settings=get_settings(),
        capabilities=state.capabilities,
        categories=state.tool_categories,
        dormant=state.dormant_summaries,
        instruction_providers=state.instruction_providers,
        availability=state.category_availability,
    )


@pytest.mark.parametrize("kind", ["task", "wake", "linked"])
async def test_the_agents_withheld_tools_reach_every_kinds_disabled_set(kind: str):
    async with client_app() as (_client, app):
        explorer = builtin_roster()["explorer"]
        applied = replace(
            as_thread(explorer, ConversationBinding()), withheld=frozenset({"files_write_file"})
        )

        disabled = await _composer(app).disabled(
            "operator", kind=kind, vision=True, applied=applied, depth=1
        )

    assert "files_write_file" in disabled
    assert DELEGATION_TOOLS <= disabled


async def _row_count(app: FastAPI) -> int:
    def work(session: Session) -> int:
        return len(list(session.exec(select(ParkedTurnRecord))))

    return await in_session(app_state(app).db_engine, work)


async def _await_rows(app: FastAPI) -> None:
    for _ in range(300):
        if await _row_count(app):
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the park never reached the store")


async def test_a_restored_chat_park_keeps_the_disabled_set_it_parked_with(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    install_sensitive_tool(monkeypatch)
    async with client_app(home=tmp_path) as (client, app):
        swap_tool_catalog(app, danger_categories())
        run_id = s(json_body(await client.post("/chat", json={"prompt": "go"}))["run_id"])
        run = await await_parked(app, run_id)
        await _await_rows(app)
        compaction = parked_turn(run).compaction
        assert compaction is not None
        fresh = compaction.deps.disabled_tools

    async with client_app(home=tmp_path) as (_client, app):
        swap_tool_catalog(app, danger_categories())
        restored = await await_parked(app, run_id)
        parked = parked_turn(restored)
        assert await recompose(app_state(app).runs, restored, parked)
        assert parked.compaction is not None
        assert parked.compaction.deps.disabled_tools == fresh


async def test_a_restored_subagent_park_keeps_its_withheld_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    install_sensitive_tool(monkeypatch)
    worker = builtin_roster()["worker"]
    async with client_app(home=tmp_path) as (_client, app):
        swap_tool_catalog(app, danger_categories())
        launcher = app_state(app).capabilities.get_optional(SubagentLauncher)
        assert launcher is not None
        started = await launcher.launch("operator", worker, "go", handle="w")
        run = await await_parked(app, started.run_id)
        await _await_rows(app)
        compaction = parked_turn(run).compaction
        assert compaction is not None
        fresh = compaction.deps.disabled_tools
        assert DELEGATION_TOOLS <= fresh

    async with client_app(home=tmp_path) as (_client, app):
        swap_tool_catalog(app, danger_categories())
        restored = await await_parked(app, started.run_id)
        parked = parked_turn(restored)
        assert await recompose(app_state(app).runs, restored, parked)
        assert parked.compaction is not None
        assert parked.compaction.deps.disabled_tools == fresh


async def test_startup_reconcile_leaves_a_parked_subagent_live(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    install_sensitive_tool(monkeypatch)
    worker = builtin_roster()["worker"]
    async with client_app(home=tmp_path) as (_client, app):
        swap_tool_catalog(app, danger_categories())
        launcher = app_state(app).capabilities.get_optional(SubagentLauncher)
        assert launcher is not None
        started = await launcher.launch("operator", worker, "go", handle="w")
        _ = await await_parked(app, started.run_id)
        await _await_rows(app)

    async with client_app(home=tmp_path) as (_client, app):
        records = app_state(app).subagent_records
        assert records is not None
        _ = await await_parked(app, started.run_id)

        assert await records.reconcile_stranded() == 0
        row = await records.get(started.subagent_id, "operator")
        assert row is not None and row.status != "cancelled"
