"""Runtime readout: versions, the model behind each role, tool counts."""

from __future__ import annotations

import platform
from importlib.metadata import version

import pytest

from core.config import get_settings
from routes.deps import app_state
from routes.runtime import ROLE_ORDER
from services import embeddings, llm

from ._helpers import client_app
from ._json import arr, json_body, obj, parse_json, s


def test_runtime_lists_every_role():
    assert sorted(ROLE_ORDER) == sorted(llm.ROLES)


async def test_runtime_reports_versions_and_unbound_roles():
    async with client_app() as (client, _app):
        body = json_body(await client.get("/runtime"))

    assert body["version"] == get_settings().version
    assert body["pydantic_ai"] == version("pydantic-ai")
    assert body["pydantic_ai_harness"] == version("pydantic-ai-harness")
    assert body["python"] == platform.python_version()
    assert body["tracing"] is True
    roles = [obj(r) for r in arr(body["roles"])]
    assert [r["role"] for r in roles] == list(ROLE_ORDER)
    assert all(r["endpoint"] is None and r["model"] is None for r in roles)


async def test_runtime_names_the_bound_endpoint_and_model(monkeypatch: pytest.MonkeyPatch):
    async def _passing_probe(*_args: object, **_kwargs: object) -> int:
        return 768

    monkeypatch.setattr(embeddings, "probe_embedding", _passing_probe)
    async with client_app() as (client, app):
        models = app_state(app).models
        chat = await models.create_endpoint(
            "operator", name="Local", base_url="http://a/v1", model="qwen"
        )
        embed = await models.create_endpoint(
            "operator", name="Embed", base_url="http://b/v1", native_tools=False
        )
        await models.set_role("operator", "utility", [chat.id])
        await models.set_role("operator", "embedding", [embed.id], model="nomic")
        body = json_body(await client.get("/runtime"))

    roles = {s(obj(r)["role"]): obj(r) for r in arr(body["roles"])}
    assert (roles["utility"]["endpoint"], roles["utility"]["model"]) == ("Local", "qwen")
    assert (roles["embedding"]["endpoint"], roles["embedding"]["model"]) == ("Embed", "nomic")
    # Unbound `main` reports the default it resolves to, flagged as such.
    assert (roles["main"]["endpoint"], roles["main"]["model"]) == ("Local", "qwen")
    assert roles["main"]["implicit"] is True
    assert roles["subagent"]["endpoint"] is None


async def test_runtime_counts_tools_the_way_the_tools_panel_lists_them():
    async with client_app() as (client, _app):
        listed = [obj(t) for t in arr(parse_json((await client.get("/tools")).content))]
        name = s(listed[0]["name"])
        _ = await client.put(f"/tools/{name}", json={"enabled": False})
        counts = obj(json_body(await client.get("/runtime"))["tools"])

    assert counts["total"] == len(listed)
    assert counts["enabled"] == len(listed) - 1
