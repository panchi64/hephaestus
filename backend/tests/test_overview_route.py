"""Home overview aggregate: real counts + backend-decided capability health."""

from __future__ import annotations

from pydantic import JsonValue

from routes.deps import app_state

from ._helpers import client_app
from ._json import arr, json_body, obj, s


def _capabilities(body: dict[str, JsonValue]) -> dict[str, dict[str, JsonValue]]:
    return {s(obj(c)["key"]): obj(c) for c in arr(body["capabilities"])}


async def test_overview_empty_workspace():
    """A fresh workspace: no endpoints/roles/conversations/memories. The chat model
    capability is unconfigured (critical alert), embeddings degrade to keyword, and
    the sandbox is absent in tests — all reported, none fabricated. The resolved
    model itself is not reported here — it's the top-bar picker's live selection."""
    async with client_app() as (client, _app):
        resp = await client.get("/overview")
        assert resp.status_code == 200
        body = json_body(resp)

    assert "main_model" not in body
    assert "main_provider" not in body
    assert "context_window" not in body
    assert body["endpoint_count"] == 0
    assert body["conversation_count"] == 0
    assert body["memory_count"] == 0
    assert body["active_run_count"] == 0

    caps = _capabilities(body)
    assert caps["chat_model"]["status"] == "alert"
    assert caps["chat_model"]["critical"] is True
    assert caps["chat_model"]["detail"] == "no provider configured"
    assert caps["chat_model"]["remediation_href"] == "/models"
    assert caps["embeddings"]["status"] == "warn"
    assert caps["sandbox"]["status"] == "warn"
    assert caps["sandbox"]["detail"] == "disabled"
    # Web search is managed (SearXNG in the platform VM); with no VM in tests it degrades
    # to warn, and there is nothing for the operator to configure.
    assert caps["web_search"]["status"] == "warn"
    assert caps["web_search"]["detail"] == "VM not ready — disabled"
    assert caps["web_search"]["remediation_href"] is None


async def test_overview_web_search_nominal_once_a_provider_is_enabled():
    async with client_app() as (client, app):
        _ = await app_state(app).search.create_provider(
            "operator", name="searx", base_url="http://searx.local", enabled=True
        )
        body = json_body(await client.get("/overview"))

    web = _capabilities(body)["web_search"]
    assert web["status"] == "nominal"
    assert web["detail"] == "SearXNG configured"


async def test_overview_chat_model_nominal_with_a_tool_calling_endpoint():
    """A native-tool-calling endpoint is the precondition for chat — no `main`
    role binding required (the picker chooses the live model)."""
    async with client_app() as (client, app):
        _ = await app_state(app).models.create_endpoint(
            "operator",
            name="Local",
            base_url="http://localhost:1234/v1",
            model="qwen2.5-32b",
            context_window=32768,
            native_tools=True,
        )
        resp = await client.get("/overview")
        assert resp.status_code == 200
        body = json_body(resp)

    assert body["endpoint_count"] == 1
    chat = _capabilities(body)["chat_model"]
    assert chat["status"] == "nominal"
    assert chat["detail"] == "1 endpoint"


async def test_overview_chat_model_alert_when_no_endpoint_is_tool_calling():
    """Endpoints exist but none drive tools — chat can't run, so the capability is
    a critical alert distinct from the no-provider-at-all case."""
    async with client_app() as (client, app):
        _ = await app_state(app).models.create_endpoint(
            "operator",
            name="Embed-only",
            base_url="http://localhost:1234/v1",
            model="nomic-embed",
            native_tools=False,
        )
        body = json_body(await client.get("/overview"))

    assert body["endpoint_count"] == 1
    chat = _capabilities(body)["chat_model"]
    assert chat["status"] == "alert"
    assert chat["critical"] is True
    assert chat["detail"] == "no tool-calling endpoint"


async def test_overview_chat_model_alert_when_only_endpoint_is_disabled():
    """Disabling the sole chat endpoint flips the critical capability to alert — a
    disabled endpoint is skipped by resolution, so it can't back chat."""
    async with client_app() as (client, app):
        models = app_state(app).models
        ep = await models.create_endpoint(
            "operator",
            name="Local",
            base_url="http://localhost:1234/v1",
            model="qwen2.5-32b",
            native_tools=True,
        )
        _ = await models.update_endpoint("operator", ep.id, enabled=False)
        body = json_body(await client.get("/overview"))

    chat = _capabilities(body)["chat_model"]
    assert chat["status"] == "alert"
    assert chat["critical"] is True
    assert chat["detail"] == "all tool-calling endpoints disabled"


async def test_overview_counts_memories_and_conversations():
    async with client_app() as (client, app):
        memory = app_state(app).memory
        _ = await memory.remember("operator", "the operator prefers dark mode")
        _ = await memory.remember("operator", "the operator's name is Frank")
        conversations = app_state(app).conversations
        _ = await conversations.create_conversation("operator")
        _ = await conversations.create_conversation("operator")
        _ = await conversations.create_conversation("operator")
        resp = await client.get("/overview")
        body = json_body(resp)

    assert body["memory_count"] == 2
    assert body["conversation_count"] == 3


async def test_overview_omits_notification_channel_rows_when_none_are_wired():
    """The rows are derived from the channels that actually exist, never fabricated. The
    app wires both by default, so this drops them to prove the rows follow the channels
    rather than being hard-coded into the aggregate."""
    async with client_app() as (client, app):
        app_state(app).notifications.channels = []
        body = json_body(await client.get("/overview"))
    caps = _capabilities(body)
    assert "email" not in caps
    assert "push" not in caps


async def test_overview_reports_unconfigured_notification_channels_as_degraded():
    """Email and push are how an unattended approval request and a reminder reach the
    operator when the app isn't open, so whether they're configured is observable —
    and neither is critical, since losing one degrades to in-app only. Both are wired by
    the app itself; a fresh workspace simply has no mailbox and no endpoint yet."""
    async with client_app() as (client, _app):
        body = json_body(await client.get("/overview"))

    caps = _capabilities(body)
    assert caps["email"]["status"] == "warn"
    assert caps["email"]["detail"] == "no mail account — in-app only"
    assert caps["email"]["critical"] is False
    assert caps["email"]["remediation_label"] == "CONFIGURE"
    assert caps["push"]["status"] == "warn"
    assert caps["push"]["detail"] == "no endpoint — in-app only"
    assert caps["push"]["critical"] is False
