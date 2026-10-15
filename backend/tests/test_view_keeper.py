"""The View history keeper bookmark (`POST /views/snapshots/{id}/keeper`): set/unset,
reflected in the conversation detail's snapshot refs, 404 on an unknown or unowned
snapshot."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from core.db import init_db, make_engine
from core.vault import Vault
from routes.deps import app_state
from services.workspace_history import WorkspaceHistoryStore

from ._helpers import client_app, collect_sse_events, patch_model_resolution
from ._json import arr, json_body, obj, s


async def _start_conversation(client: httpx.AsyncClient) -> str:
    resp = await client.post("/chat", json={"prompt": "hi"})
    body = json_body(resp)
    _ = await collect_sse_events(client, s(body["run_id"]))
    return s(body["conversation_id"])


async def _first_snapshot(client: httpx.AsyncClient, conv: str) -> dict[str, object]:
    detail = json_body(await client.get(f"/conversations/{conv}"))
    return dict(obj(arr(detail["snapshots"])[0]))


async def test_set_keeper_204_and_reflected_in_conversation_detail(
    monkeypatch: pytest.MonkeyPatch,
):
    patch_model_resolution(monkeypatch, output_text="hi")
    async with client_app() as (client, app):
        conv = await _start_conversation(client)
        snap = await app_state(app).workspace_history.capture(
            "operator", conv, run_id="r1", files={"a.py": b"x"}
        )
        # Not yet a keeper.
        assert (await _first_snapshot(client, conv))["keeper"] is False

        resp = await client.post(f"/views/snapshots/{snap.id}/keeper", json={"keeper": True})
        assert resp.status_code == 204

        assert (await _first_snapshot(client, conv))["keeper"] is True


async def test_unset_keeper(monkeypatch: pytest.MonkeyPatch):
    patch_model_resolution(monkeypatch, output_text="hi")
    async with client_app() as (client, app):
        conv = await _start_conversation(client)
        snap = await app_state(app).workspace_history.capture(
            "operator", conv, run_id="r1", files={"a.py": b"x"}
        )
        _ = await client.post(f"/views/snapshots/{snap.id}/keeper", json={"keeper": True})

        resp = await client.post(f"/views/snapshots/{snap.id}/keeper", json={"keeper": False})
        assert resp.status_code == 204

        assert (await _first_snapshot(client, conv))["keeper"] is False


async def test_keeper_unknown_snapshot_404():
    async with client_app() as (client, _app):
        resp = await client.post("/views/snapshots/nope/keeper", json={"keeper": True})
        assert resp.status_code == 404


async def test_keeper_wrong_owner_404(tmp_path: Path):
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    vault = Vault(tmp_path / "keyfile.json")
    await vault.setup("pw")
    store = WorkspaceHistoryStore(engine, vault)
    snap = await store.capture("owner-a", "conv-1", run_id="r1", files={"a.py": b"x"})

    assert await store.set_keeper("owner-b", snap.id, True) is False
