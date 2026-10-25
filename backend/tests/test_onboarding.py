"""The first-run walkthrough: when it is owed, what it refuses to finish without, and the
local-server scan it opens with."""

from __future__ import annotations

import httpx
import pytest

from core.db import init_db, make_engine
from services import llm, onboarding
from services.settings_store import SettingsStore
from tests._helpers import client_app
from tests._json import json_body, json_list, s


async def _ok_probe(*_args: object, **_kwargs: object) -> None:
    return None


async def _no_limits(*_args: object, **_kwargs: object) -> tuple[int | None, int | None]:
    return None, None


@pytest.fixture(autouse=True)
def _offline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(llm, "probe_openai_endpoint", _ok_probe)
    monkeypatch.setattr(llm, "discover_openai_limits", _no_limits)


async def _add_endpoint(client: httpx.AsyncClient, *, context_window: int | None) -> str:
    ep = json_body(
        await client.post(
            "/models/endpoints",
            json={
                "name": "local",
                "base_url": "http://x/v1",
                "model": "m",
                "context_window": context_window,
            },
        )
    )
    return s(ep["id"])


async def test_a_workspace_without_the_key_counts_as_onboarded():
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    store = SettingsStore(engine)
    assert await onboarding.is_completed(store, "op") is True
    await onboarding.mark_pending(store, "op")
    assert await onboarding.is_completed(store, "op") is False


async def test_first_run_setup_owes_the_walkthrough():
    async with client_app(auth_enabled=True, passphrase=None) as (client, _app):
        assert (await client.get("/onboarding")).status_code == 401
        _ = await client.post("/setup", json={"password": "correct horse"})
        assert json_body(await client.get("/onboarding")) == {"completed": False}


async def test_boot_time_setup_owes_the_walkthrough():
    async with client_app() as (client, _app):
        assert json_body(await client.get("/onboarding")) == {"completed": False}


async def test_complete_refuses_until_the_workspace_can_hold_a_conversation():
    async with client_app() as (client, _app):
        refused = await client.post("/onboarding/complete")
        assert refused.status_code == 409
        assert "connection test" in s(json_body(refused)["detail"])

        endpoint_id = await _add_endpoint(client, context_window=None)
        _ = await client.post(f"/models/endpoints/{endpoint_id}/test")
        refused = await client.post("/onboarding/complete")
        assert "No chat model" in s(json_body(refused)["detail"])

        bound = await client.put(
            "/models/roles/main", json={"endpoint_ids": [endpoint_id], "model": "m"}
        )
        assert bound.status_code == 204
        refused = await client.post("/onboarding/complete")
        assert refused.status_code == 409
        assert "context window" in s(json_body(refused)["detail"])

        _ = await client.patch(f"/models/endpoints/{endpoint_id}", json={"context_window": 32768})
        done = await client.post("/onboarding/complete")
        assert done.status_code == 200
        assert json_body(await client.get("/onboarding")) == {"completed": True}


async def test_a_disabled_endpoint_does_not_satisfy_the_endpoint_step():
    async with client_app() as (client, _app):
        endpoint_id = await _add_endpoint(client, context_window=32768)
        _ = await client.post(f"/models/endpoints/{endpoint_id}/test")
        _ = await client.patch(f"/models/endpoints/{endpoint_id}", json={"enabled": False})
        assert (await client.post("/onboarding/complete")).status_code == 409


async def test_reopen_owes_the_walkthrough_again():
    async with client_app() as (client, _app):
        endpoint_id = await _add_endpoint(client, context_window=32768)
        _ = await client.post(f"/models/endpoints/{endpoint_id}/test")
        _ = await client.put(
            "/models/roles/main", json={"endpoint_ids": [endpoint_id], "model": "m"}
        )
        assert (await client.post("/onboarding/complete")).status_code == 200
        assert json_body(await client.post("/onboarding/reopen")) == {"completed": False}
        assert json_body(await client.get("/onboarding")) == {"completed": False}


async def test_local_scan_reports_the_servers_that_answer(monkeypatch: pytest.MonkeyPatch):
    async def probe(base_url: str, *_args: object, **_kwargs: object) -> None:
        if ":8080" in base_url:
            raise httpx.ConnectError("refused")

    async def models(*_args: object, **_kwargs: object) -> list[str]:
        return ["nomic-embed", "qwen3"]

    async def listing(*_args: object, **_kwargs: object) -> dict[str, dict[str, object]]:
        return {"qwen3": {"state": "loaded", "type": "llm"}, "nomic-embed": {"type": "embeddings"}}

    async def limits(
        _base: str, model: str, *_a: object, **_k: object
    ) -> tuple[int | None, int | None]:
        return (65536, 4) if model == "qwen3" else (None, None)

    monkeypatch.setattr(llm, "probe_openai_endpoint", probe)
    monkeypatch.setattr(llm, "discover_openai_models", models)
    monkeypatch.setattr(llm, "lmstudio_listing", listing)
    monkeypatch.setattr(llm, "discover_openai_limits", limits)
    async with client_app() as (client, _app):
        found = json_list(await client.get("/onboarding/local-endpoints"))
    assert found == [
        {
            "label": "LM Studio",
            "base_url": "http://127.0.0.1:1234/v1",
            "models": [
                {
                    "id": "nomic-embed",
                    "context_window": None,
                    "parallel_slots": None,
                    "loaded": None,
                    "kind": "embeddings",
                },
                {
                    "id": "qwen3",
                    "context_window": 65536,
                    "parallel_slots": 4,
                    "loaded": True,
                    "kind": "llm",
                },
            ],
        }
    ]
