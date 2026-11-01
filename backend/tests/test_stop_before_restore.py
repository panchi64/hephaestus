"""The idle-thread stop waits for stored parks to be restored, so it reaches a parked child."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from routes import deps
from routes.deps import OPERATOR_ID, app_state
from runs import RunStatus
from services.agents import builtin_roster
from services.parked_turns import SealedParkedStore, StoredPark
from services.subagents import SubagentLauncher
from services.subagents.launcher import SubagentParent

from ._helpers import await_parked, client_app, swap_tool_catalog
from .test_approval_routes import danger_categories, install_sensitive_tool
from .test_parked_turns import await_row


async def test_stop_waits_for_the_restore_and_then_stops_the_parked_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    install_sensitive_tool(monkeypatch)
    worker = builtin_roster()["worker"]
    async with client_app(home=tmp_path) as (_client, app):
        swap_tool_catalog(app, danger_categories())
        parent = await deps.store(app).create_conversation(OPERATOR_ID)
        launcher = app_state(app).capabilities.get_optional(SubagentLauncher)
        assert launcher is not None
        started = await launcher.launch(
            "operator",
            worker,
            "delete it",
            handle="deleter",
            parent=SubagentParent(conversation_id=parent),
        )
        _ = await await_parked(app, started.run_id)
        await await_row(app, started.run_id)

    gate = asyncio.Event()
    real_load = SealedParkedStore.load

    async def delayed_load(self: SealedParkedStore) -> list[StoredPark]:
        _ = await gate.wait()
        return await real_load(self)

    monkeypatch.setattr(SealedParkedStore, "load", delayed_load)
    async with client_app(home=tmp_path) as (client, app):
        swap_tool_catalog(app, danger_categories())
        stopping = asyncio.create_task(client.post(f"/conversations/{parent}/agents/stop"))
        await asyncio.sleep(0.2)
        assert not stopping.done()
        gate.set()
        resp = await stopping
        assert resp.status_code == 200
        assert resp.json()["stopped"] == ["deleter"]
        run = app_state(app).runs.get(started.run_id)
        assert run is not None
        assert run.status is not RunStatus.awaiting_input
