"""The provider layer: adapter dispatch, save-time validation, and the migration that
backfills the legacy name-prefix inference into a real ``provider`` column."""

from __future__ import annotations

import pytest
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.models.openai import OpenAIChatModel
from sqlmodel import Session, text

from core.db import make_engine
from services import llm
from services.endpoint_spec import EndpointSpec
from services.providers import all_providers, get_provider
from tests.test_registry import make_registry, resolve_model

from ._helpers import client_app
from ._json import json_body, json_list, obj, s

OWNER = "operator"


# --- the adapter registry --------------------------------------------------


def test_builtin_providers_are_discovered():
    ids = {p.id for p in all_providers()}
    assert {"openai-compatible", "anthropic", "google"} <= ids


def test_unknown_provider_raises():
    with pytest.raises(LookupError):
        _ = get_provider("no-such-lab")


def test_build_model_dispatches_to_the_native_classes():
    """The whole point of the layer: a provider id selects the concrete Pydantic AI
    model class, so a lab's native protocol is spoken natively."""
    cases = [
        ("openai-compatible", OpenAIChatModel),
        ("anthropic", AnthropicModel),
        ("google", GoogleModel),
    ]
    for provider_id, model_cls in cases:
        spec = EndpointSpec(
            base_url="https://api.example.com",
            model="some-model",
            provider=provider_id,
            api_key="k",
        )
        assert isinstance(llm.build_model(spec), model_cls), provider_id


async def test_openai_wire_carries_one_leading_system_message():
    """The standing prompt and the per-turn instructions both ride the OpenAI wire as
    system messages at the head. Hosted APIs take two; a self-hosted server hands the list
    to the model's own chat template, and the Qwen family's raises "System message must be
    at the beginning." on the second — so the adapter merges them into one. Asserted on
    the mapped wire messages (the library's private mapper) because the profile flag
    alone wouldn't catch the day the library renames or re-scopes it."""
    from pydantic_ai import ModelRequest, SystemPromptPart, UserPromptPart
    from pydantic_ai.models import ModelRequestParameters

    history = [
        ModelRequest(
            parts=[SystemPromptPart(content="IDENTITY"), UserPromptPart(content="hi")],
            instructions="RULES",
        )
    ]
    spec = EndpointSpec(
        base_url="http://127.0.0.1:8080/v1",
        model="Qwen3-30B",
        provider="openai-compatible",
        api_key=None,
    )
    model = llm.build_model(spec)
    assert isinstance(model, OpenAIChatModel)
    mapped = await model._map_messages(history, ModelRequestParameters())
    assert [m["role"] for m in mapped] == ["system", "user"]
    assert mapped[0].get("content") == "IDENTITY\n\nRULES"


def test_reasoning_off_is_provider_shaped():
    from services.reasoning import ModelDescriptor

    # OpenAI-compat falls back to the model-name heuristics.
    openai = get_provider("openai-compatible")
    assert openai.reasoning_off(ModelDescriptor(model_id="qwen-3")) != {}
    assert openai.reasoning_off(ModelDescriptor(model_id="llama-3.1-70b")) == {}
    # Anthropic thinking is opt-in per request — nothing to turn off.
    assert get_provider("anthropic").reasoning_off(ModelDescriptor(model_id="claude-x")) == {}
    # Only the Gemini Flash/Lite family accepts a zero budget; Pro rejects it.
    google = get_provider("google")
    assert google.reasoning_off(ModelDescriptor(model_id="gemini-2.5-flash")) == {
        "google_thinking_config": {"thinking_budget": 0}
    }
    assert google.reasoning_off(ModelDescriptor(model_id="gemini-2.5-pro")) == {}


# --- save-time validation ---------------------------------------------------


async def test_key_requiring_provider_rejects_a_keyless_save():
    reg = await make_registry()
    with pytest.raises(ValueError):
        _ = await reg.create_endpoint(
            OWNER, name="a", base_url="https://api.anthropic.com", provider="anthropic"
        )
    # And clearing the key out from under one is rejected the same way.
    ep = await reg.create_endpoint(
        OWNER,
        name="b",
        base_url="https://api.anthropic.com",
        provider="anthropic",
        model="claude-x",
        api_key="sk-ant-k",
    )
    with pytest.raises(ValueError):
        _ = await reg.update_endpoint(OWNER, ep.id, api_key="")
    # Switching a keyless endpoint onto a key-requiring provider is rejected too.
    keyless = await reg.create_endpoint(OWNER, name="c", base_url="http://x/v1", model="m")
    with pytest.raises(ValueError):
        _ = await reg.update_endpoint(OWNER, keyless.id, provider="google")


async def test_unknown_provider_rejected_at_save():
    reg = await make_registry()
    with pytest.raises(ValueError):
        _ = await reg.create_endpoint(
            OWNER, name="a", base_url="http://x/v1", provider="no-such-lab"
        )


async def test_provider_flows_into_the_resolved_spec():
    reg = await make_registry()
    ep = await reg.create_endpoint(
        OWNER,
        name="claude",
        base_url="https://api.anthropic.com",
        provider="anthropic",
        model="claude-x",
        api_key="sk-ant-k",
    )
    await reg.set_role(OWNER, "main", [ep.id])
    model = await resolve_model(reg, "main", owner_id=OWNER)
    assert isinstance(model, AnthropicModel)


# --- the surface --------------------------------------------------------------


async def test_providers_route_serves_the_presets():
    async with client_app() as (client, _app):
        rows = json_list(await client.get("/models/providers"))
        by_id = {s(obj(row)["id"]): obj(row) for row in rows}
        assert {"openai-compatible", "anthropic", "google"} <= set(by_id)
        anthropic = by_id["anthropic"]
        assert anthropic["requires_key"] is True
        assert anthropic["default_base_url"] == "https://api.anthropic.com"
        assert by_id["openai-compatible"]["requires_key"] is False


async def test_endpoint_routes_carry_and_validate_provider():
    async with client_app() as (client, _app):
        rejected = await client.post(
            "/models/endpoints",
            json={"name": "a", "base_url": "https://api.anthropic.com", "provider": "anthropic"},
        )
        assert rejected.status_code == 422

        created = await client.post(
            "/models/endpoints",
            json={
                "name": "a",
                "base_url": "https://api.anthropic.com",
                "provider": "anthropic",
                "model": "claude-x",
                "api_key": "sk-ant-k",
            },
        )
        assert created.status_code == 201
        body = json_body(created)
        assert body["provider"] == "anthropic"

        # Default provider when unspecified.
        plain = json_body(
            await client.post("/models/endpoints", json={"name": "b", "base_url": "http://x/v1"})
        )
        assert plain["provider"] == "openai-compatible"


# --- the migration's backfill -------------------------------------------------


def test_migration_backfills_local_prefix_endpoints():
    """Build the DB at the pre-provider revision, insert a locally-served endpoint the
    old way (its provider inferable only from a "Local · " name prefix, liveness
    overloaded onto `enabled`), upgrade to head, and assert the backfill turned that
    inference into a real `provider` column — and handed `enabled` back to the
    operator."""
    from alembic import command
    from alembic.config import Config

    from core.db import ALEMBIC_INI

    engine = make_engine("sqlite:///:memory:")
    config = Config(str(ALEMBIC_INI))
    config.attributes["connection"] = engine
    command.upgrade(config, "5a07sec0007")

    with Session(engine) as session:
        _ = session.connection().execute(
            text(
                "".join(
                    [
                        "INSERT INTO model_endpoints ",
                        "(id, owner_id, name, base_url, enabled, native_tools, vision, thinking, ",
                        "created_at, updated_at) VALUES ",
                        "('e1', 'op', 'Local · acme/m', 'http://127.0.0.1:9/v1', 0, 1, 0, 0, ",
                        "'2026-01-01', '2026-01-01'), ",
                        "('e2', 'op', 'My cloud', 'https://api.example.com/v1', 1, 1, 0, 0, ",
                        "'2026-01-01', '2026-01-01')",
                    ]
                )
            )
        )
        session.commit()

    command.upgrade(config, "head")

    columns = "provider, enabled"
    with Session(engine) as session:
        connection = session.connection()
        select = f"SELECT {columns} FROM model_endpoints WHERE id="
        local = connection.execute(text(f"{select}'e1'")).one()
        cloud = connection.execute(text(f"{select}'e2'")).one()
    # The row that was off only because it wasn't running: `enabled` handed back on, and
    # the retired `local` provider folded into the OpenAI-compatible one it always was.
    assert tuple(local) == ("openai-compatible", 1)
    assert tuple(cloud) == ("openai-compatible", 1)


# ── The context window on the model's profile ────────────────────────────────────


def _window(provider_id: str, model: str, window: int | None) -> int | None:
    spec = EndpointSpec(
        base_url="https://api.example.com",
        model=model,
        provider=provider_id,
        api_key="k",
        context_window=window,
    )
    return llm.build_model(spec).profile.get("context_window")


def test_the_endpoints_own_window_wins_for_every_provider():
    """The registry funnels every resolution through `with_model_limits`, which
    prefers the operator's figure and otherwise probes the server. Whatever it lands on
    is what the model is built with — a library that resolved a different number from the
    model's name would give the gauge and the fold trigger a second source of truth."""
    for provider_id in ("openai-compatible", "anthropic", "google"):
        assert _window(provider_id, "gpt-5", 8192) == 8192, provider_id


def test_an_openai_wire_endpoint_with_no_window_declares_none():
    """`gpt-5` names a 400k model and nothing else — this adapter fronts *any* server, so
    the name is a coincidence of what the operator typed into a box. A llama.cpp instance
    started with `-c 8192` behind that name must not be described as holding 400k; unknown
    is the honest answer, and the one the fold trigger already handles by never firing."""
    assert _window("openai-compatible", "gpt-5", None) is None
    assert _window("openai-compatible", "some-local-thing", None) is None


def test_a_hosted_endpoint_with_no_window_keeps_the_librarys_figure():
    """The other side of the same rule. These adapters talk to one lab, so the model name
    really does name that model — and where discovery came back empty (an unreachable
    models API, a proxy that doesn't say) the library's figure is the only one there is.
    Discarding it would leave a hosted thread with no gauge at all."""
    assert _window("anthropic", "claude-sonnet-4-5", None) is not None


# ── The output ceiling on the model's settings ───────────────────────────────────


def _anthropic(max_output: int | None):
    return llm.build_model(
        EndpointSpec(
            base_url="https://api.anthropic.com",
            model="claude-x",
            provider="anthropic",
            api_key="k",
            max_output_tokens=max_output,
        )
    )


def test_an_anthropic_models_own_output_ceiling_replaces_the_librarys_4096():
    """`AnthropicModel` sends 4096 whenever no layer supplied `max_tokens`, cutting every
    unbounded answer and summary short. The discovered ceiling sits on the model's own
    settings, so it holds through a fallback chain and a parked resume."""
    assert (_anthropic(128_000).settings or {}).get("max_tokens") == 128_000


def test_an_unknown_ceiling_leaves_max_tokens_absent():
    assert "max_tokens" not in (_anthropic(None).settings or {})


def test_a_requests_own_max_tokens_still_wins():
    from pydantic_ai.settings import merge_model_settings

    merged = merge_model_settings(_anthropic(128_000).settings, {"max_tokens": 512})
    assert merged is not None and merged.get("max_tokens") == 512


def test_the_other_wires_carry_no_output_ceiling():
    for provider_id in ("openai-compatible", "google"):
        spec = EndpointSpec(
            base_url="https://api.example.com",
            model="m",
            provider=provider_id,
            api_key="k",
            max_output_tokens=128_000,
        )
        assert "max_tokens" not in (llm.build_model(spec).settings or {}), provider_id


def test_the_openai_wire_still_merges_leading_system_messages():
    """The window is a *second* field on that adapter's partial profile — the merge flag
    it has always set must survive being joined by one."""
    spec = EndpointSpec(
        base_url="http://127.0.0.1:8080/v1",
        model="Qwen3-30B",
        provider="openai-compatible",
        api_key=None,
        context_window=8192,
    )
    profile = llm.build_model(spec).profile
    assert profile.get("openai_chat_supports_multiple_system_messages") is False
    assert profile.get("context_window") == 8192
