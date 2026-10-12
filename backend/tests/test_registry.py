"""The model registry: role→chain resolution, encryption at rest, REST surface."""

from __future__ import annotations

import tempfile
from pathlib import Path

import httpx
import pytest
from pydantic_ai.models import Model
from pydantic_ai.models.fallback import FallbackModel
from pydantic_ai.models.openai import OpenAIChatModel
from sqlmodel import Session, select

from core.db import in_session, init_db, make_engine
from core.exceptions import DegradedCapabilityError, NotFoundError
from core.vault import Vault
from models.registry import ModelRole
from runs import Endpoint, EndpointGate
from services import embeddings, llm
from services.gated_model import EndpointGatedModel
from services.registry import ModelRegistry

from ._helpers import client_app

OWNER = "operator"


async def _passing_probe(spec) -> int:
    """A stub embedding probe that accepts any binding — lets role tests bind the
    embedding role without a live ``/embeddings`` server."""
    return 4


async def _registry() -> ModelRegistry:
    """A registry on a throwaway in-memory DB with an unlocked vault."""
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    tmp = tempfile.mkdtemp()
    vault = Vault(Path(tmp) / "keyfile.json")
    await vault.setup("pw")
    return ModelRegistry(engine, vault)


async def _resolve(reg: ModelRegistry, role: str, **overrides) -> Model:
    """The chain ``role`` resolves to, reached the way production reaches it.

    Every caller — chat, research, the scheduler, titling — goes through
    ``resolve_detailed`` (or ``resolve_background``, which delegates to it), so these
    tests do too rather than through a model-only entry point nothing ships. The
    assertions below are about the built chain, so they take ``.model`` off the result;
    the reasoning-off settings that travel with it are covered separately."""
    return (await reg.resolve_detailed(role, **overrides)).model


async def test_single_endpoint_resolves_to_plain_model():
    reg = await _registry()
    ep = await reg.create_endpoint(OWNER, name="local", base_url="http://x/v1", model="qwen")
    await reg.set_role(OWNER, "main", [ep.id])

    model = await _resolve(reg, "main", owner_id=OWNER)
    assert isinstance(model, OpenAIChatModel)
    assert not isinstance(model, FallbackModel)


async def test_multi_endpoint_chain_wraps_in_fallback():
    reg = await _registry()
    primary = await reg.create_endpoint(OWNER, name="a", base_url="http://a/v1", model="m1")
    backup = await reg.create_endpoint(OWNER, name="b", base_url="http://b/v1", model="m2")
    await reg.set_role(OWNER, "main", [primary.id, backup.id])

    model = await _resolve(reg, "main", owner_id=OWNER)
    assert isinstance(model, FallbackModel)
    assert len(model.models) == 2


async def test_a_gated_registry_wraps_the_primary_and_names_its_capacity():
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    vault = Vault(Path(tempfile.mkdtemp()) / "keyfile.json")
    await vault.setup("pw")
    reg = ModelRegistry(engine, vault, gate=EndpointGate())
    primary = await reg.create_endpoint(
        OWNER, name="a", base_url="http://a/v1", model="m1", max_concurrent_runs=2
    )
    backup = await reg.create_endpoint(OWNER, name="b", base_url="http://b/v1", model="m2")
    await reg.set_role(OWNER, "main", [primary.id, backup.id])

    resolved = await reg.resolve_detailed("main", owner_id=OWNER)
    assert resolved.endpoint == Endpoint(id=primary.id, name="a", capacity=2)
    assert isinstance(resolved.model, FallbackModel)
    head, tail = resolved.model.models
    assert isinstance(head, EndpointGatedModel)
    assert isinstance(tail, OpenAIChatModel)

    await reg.update_endpoint(OWNER, primary.id, max_concurrent_runs=0)
    cleared = await reg.resolve_detailed("main", owner_id=OWNER)
    assert cleared.endpoint is not None
    assert cleared.endpoint.capacity is None


async def test_unbound_utility_is_degraded():
    reg = await _registry()
    ep = await reg.create_endpoint(OWNER, name="main-ep", base_url="http://m/v1", model="m")
    await reg.set_role(OWNER, "main", [ep.id])
    # utility no longer inherits main's chain — an unbound utility is degraded;
    # the chat layer reuses the resolved main model instead.
    with pytest.raises(DegradedCapabilityError):
        await _resolve(reg, "utility", owner_id=OWNER)


async def test_bound_utility_resolves_independently():
    reg = await _registry()
    ep = await reg.create_endpoint(OWNER, name="util", base_url="http://u/v1", model="u")
    await reg.set_role(OWNER, "utility", [ep.id])
    model = await _resolve(reg, "utility", owner_id=OWNER)
    assert isinstance(model, OpenAIChatModel)


async def test_main_override_picks_a_specific_endpoint():
    reg = await _registry()
    default = await reg.create_endpoint(OWNER, name="d", base_url="http://d/v1", model="d")
    picked = await reg.create_endpoint(OWNER, name="p", base_url="http://p/v1", model="p")
    await reg.set_role(OWNER, "main", [default.id])

    model = await _resolve(reg, "main", owner_id=OWNER, override_endpoint_id=picked.id)
    assert isinstance(model, OpenAIChatModel)
    assert model.model_name == "p"


async def test_main_override_with_model_picks_that_model():
    reg = await _registry()
    # The endpoint is a bare provider connection (no baked model); the picker
    # supplies the specific model it discovered from the provider.
    ep = await reg.create_endpoint(OWNER, name="p", base_url="http://p/v1")
    model = await _resolve(
        reg, "main", owner_id=OWNER, override_endpoint_id=ep.id, override_model="qwen-72b"
    )
    assert isinstance(model, OpenAIChatModel)
    assert model.model_name == "qwen-72b"


async def test_modelless_endpoint_without_override_is_degraded():
    reg = await _registry()
    ep = await reg.create_endpoint(OWNER, name="p", base_url="http://p/v1")
    await reg.set_role(OWNER, "main", [ep.id])
    # No baked model and no picker override → nothing to run.
    with pytest.raises(DegradedCapabilityError):
        await _resolve(reg, "main", owner_id=OWNER)


async def test_role_pinned_model_resolves_a_modelless_endpoint():
    """A discovery-only endpoint (no default model) is resolvable once the role pins
    a model — the stored binding is self-describing, so resolution needs no
    per-conversation override. This is the fact server-initiated callers (research,
    tasks, titling) depend on, since they never carry the picker's override."""
    reg = await _registry()
    ep = await reg.create_endpoint(OWNER, name="p", base_url="http://p/v1")  # no model
    await reg.set_role(OWNER, "main", [ep.id], model="qwen3-32b")

    model = await _resolve(reg, "main", owner_id=OWNER)  # no override
    assert isinstance(model, OpenAIChatModel)
    assert model.model_name == "qwen3-32b"

    # Same for a bound utility on a modelless endpoint.
    util = await reg.create_endpoint(OWNER, name="u", base_url="http://u/v1")
    await reg.set_role(OWNER, "utility", [util.id], model="qwen3-4b")
    util_model = await _resolve(reg, "utility", owner_id=OWNER)
    assert isinstance(util_model, OpenAIChatModel)
    assert util_model.model_name == "qwen3-4b"


async def test_role_pinned_model_applies_to_head_only_in_a_chain():
    """The role's single pinned model names a model on the head provider, so it
    applies to the chain head only; a fallback tail keeps its own default."""
    reg = await _registry()
    head = await reg.create_endpoint(OWNER, name="head", base_url="http://h/v1")  # no default
    tail = await reg.create_endpoint(OWNER, name="tail", base_url="http://t/v1", model="tail-m")
    await reg.set_role(OWNER, "utility", [head.id, tail.id], model="head-pinned")

    model = await _resolve(reg, "utility", owner_id=OWNER)
    assert isinstance(model, FallbackModel)
    assert [m.model_name for m in model.models] == ["head-pinned", "tail-m"]


async def test_resolve_background_uses_main_pinned_model_when_utility_unbound():
    """The reported crash path: utility unbound, main on a discovery-only endpoint.
    Background resolution degrades utility→main and must pick up main's pinned model
    — no override in play — instead of raising 'no model configured'."""
    reg = await _registry()
    ep = await reg.create_endpoint(OWNER, name="m", base_url="http://m/v1")  # no default model
    await reg.set_role(OWNER, "main", [ep.id], model="qwen3-32b")

    resolved = await reg.resolve_background(owner_id=OWNER)  # no override — as research calls it
    assert isinstance(resolved.model, OpenAIChatModel)
    assert resolved.model.model_name == "qwen3-32b"


async def test_unconfigured_role_is_degraded():
    reg = await _registry()
    # No endpoints, no bindings → degraded; the registry is the only source of
    # truth, so there is no env (or other) fallback to rescue resolution.
    with pytest.raises(DegradedCapabilityError):
        await _resolve(reg, "main", owner_id=OWNER)
    with pytest.raises(DegradedCapabilityError):
        await _resolve(reg, "embedding", owner_id=OWNER)


async def test_api_key_is_encrypted_at_rest():
    reg = await _registry()
    ep = await reg.create_endpoint(
        OWNER, name="keyed", base_url="http://x/v1", model="m", api_key="super-secret"
    )
    assert ep.api_key_enc is not None
    assert "super-secret" not in ep.api_key_enc  # stored as ciphertext
    # And it round-trips on resolve: the built provider gets the plaintext key.
    await reg.set_role(OWNER, "main", [ep.id])
    model = await _resolve(reg, "main", owner_id=OWNER)
    assert isinstance(model, OpenAIChatModel)


async def test_tool_calling_role_rejects_non_tool_endpoint(monkeypatch):
    reg = await _registry()
    ep = await reg.create_endpoint(
        OWNER, name="no-tools", base_url="http://x/v1", model="m", native_tools=False
    )
    with pytest.raises(ValueError, match="native tool-calling"):
        await reg.set_role(OWNER, "main", [ep.id])
    # An embedding role accepts it — tool-calling isn't required there. (The bind-time
    # probe is the *embeddings*-capability check, separate from tool-calling; stub it.)
    monkeypatch.setattr(embeddings, "probe_embedding", _passing_probe)
    await reg.set_role(OWNER, "embedding", [ep.id])


async def test_embedding_role_rejects_a_model_that_serves_no_vectors(monkeypatch):
    # Binding the embedding role probes the endpoint; a model that doesn't return a
    # vector (e.g. a chat model bound by mistake) is rejected up front, not silently
    # degraded to keyword-only recall.
    reg = await _registry()
    ep = await reg.create_endpoint(OWNER, name="chat", base_url="http://x/v1", model="gpt-4o")

    async def _failing_probe(spec):
        raise DegradedCapabilityError(f"model {spec.model!r} returned no vector")

    monkeypatch.setattr(embeddings, "probe_embedding", _failing_probe)
    with pytest.raises(ValueError, match="no vector"):
        await reg.set_role(OWNER, "embedding", [ep.id])
    # Nothing was bound — the failed probe left the role unset.
    assert await reg.get_role(OWNER, "embedding") == []


async def test_embedding_role_persists_the_picked_model(monkeypatch):
    # The embedding role pins an explicit model on the endpoint (its stand-in for
    # main's per-conversation picker); the probe runs against that model and the
    # choice is persisted for resolution.
    reg = await _registry()
    ep = await reg.create_endpoint(OWNER, name="multi", base_url="http://x/v1", model="default")

    seen: dict = {}

    async def _record_probe(spec):
        seen["model"] = spec.model
        return 4

    monkeypatch.setattr(embeddings, "probe_embedding", _record_probe)
    await reg.set_role(OWNER, "embedding", [ep.id], model="text-embed-3")
    assert seen["model"] == "text-embed-3"  # probed the picked model, not the default
    assert await reg.get_role_model(OWNER, "embedding") == "text-embed-3"
    spec = await reg.resolve_embedding_spec(OWNER)
    assert spec.model == "text-embed-3"  # resolution honors the pinned model


async def test_an_unreachable_embedding_endpoint_degrades_instead_of_raising(monkeypatch):
    """A local model server that isn't running is the ordinary case, and every caller
    already handles "degraded" by falling back to keyword recall. Raised raw it instead
    buries a connection traceback in the log on every persisted turn — and never names
    the address, which is the one fact needed to fix it. The bind-time probe already
    draws this line; runtime has to draw it the same way."""
    reg = await _registry()
    ep = await reg.create_endpoint(
        OWNER, name="local", base_url="http://127.0.0.1:9/v1", model="embed"
    )
    monkeypatch.setattr(embeddings, "probe_embedding", _passing_probe)
    await reg.set_role(OWNER, "embedding", [ep.id])

    with pytest.raises(DegradedCapabilityError, match="127.0.0.1:9"):
        await embeddings.RegistryEmbedder(reg).embed(OWNER, ["hello"])


async def test_unknown_endpoint_in_chain_is_not_found():
    reg = await _registry()
    with pytest.raises(NotFoundError):
        await reg.set_role(OWNER, "main", ["does-not-exist"])


async def test_resolve_background_falls_back_to_picked_main_when_utility_unbound():
    """Background work (titling) resolves ``utility``, but an operator who never bound
    it must still get a model — the picker's ``main`` override. The reasoning-off
    settings travel with whichever resolves."""
    reg = await _registry()
    ep = await reg.create_endpoint(OWNER, name="m", base_url="http://m/v1", model="qwen3")
    # No utility role bound; the override supplies main directly (the picker-driven
    # operator with no default chain — the exact case the retitle 503 came from).
    resolved = await reg.resolve_background(
        owner_id=OWNER, override_endpoint_id=ep.id, override_model="qwen3"
    )
    assert resolved.model is not None
    assert resolved.reasoning_off is not None


async def test_delete_endpoint_prunes_it_from_role_chains():
    """Deleting an endpoint must not leave a dangling id in any chain that
    referenced it — otherwise a later resolve trips on the missing endpoint."""
    reg = await _registry()
    primary = await reg.create_endpoint(OWNER, name="a", base_url="http://a/v1", model="m1")
    backup = await reg.create_endpoint(OWNER, name="b", base_url="http://b/v1", model="m2")
    await reg.set_role(OWNER, "main", [primary.id, backup.id])

    await reg.delete_endpoint(OWNER, backup.id)

    assert await reg.get_role(OWNER, "main") == [primary.id]


async def test_main_context_window_survives_a_stale_chain():
    """A dangling id in the ``main`` chain degrades the context meter to None
    rather than raising — the conversation read must not 500 on it."""
    reg = await _registry()
    ep = await reg.create_endpoint(
        OWNER, name="local", base_url="http://x/v1", model="qwen", context_window=8192
    )
    await reg.set_role(OWNER, "main", [ep.id])
    assert await reg.main_context_window(OWNER) == 8192

    # Simulate a chain left pointing at an endpoint that no longer exists (an
    # out-of-band delete, or a pre-prune dangling reference).
    def plant_stale(session: Session) -> None:
        binding = session.exec(select(ModelRole).where(ModelRole.role == "main")).one()
        binding.endpoint_ids = ["a296928be47b4011ba15a9b806fb31e4"]
        session.add(binding)

    await in_session(reg._engine, plant_stale)
    assert await reg.main_context_window(OWNER) is None


# --- REST surface ---------------------------------------------------------


async def test_endpoint_crud_over_rest_hides_api_key():
    async with client_app() as (client, _app):
        resp = await client.post(
            "/models/endpoints",
            json={"name": "local", "base_url": "http://x/v1", "model": "m", "api_key": "k"},
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["has_api_key"] is True
        assert "api_key" not in body and "api_key_enc" not in body
        endpoint_id = body["id"]

        listing = (await client.get("/models/endpoints")).json()
        assert [e["id"] for e in listing] == [endpoint_id]

        # Bind it to the main role, then read the bindings back.
        put = await client.put("/models/roles/main", json={"endpoint_ids": [endpoint_id]})
        assert put.status_code == 204
        roles = (await client.get("/models/roles")).json()
        # `context_window` is null here for two compounding reasons — no override was
        # set and the fake host can't be asked — which is exactly the state the send
        # gate refuses a turn in.
        # `implicit` is False because the operator wrote this binding — the flag
        # distinguishes that from the default the backend resolves when nothing is
        # bound at all. `background_shares_endpoint` is True because `utility` is unbound
        # and so degrades onto this very endpoint.
        assert roles == {
            "main": {
                "endpoint_ids": [endpoint_id],
                "model": None,
                "context_window": None,
                "implicit": False,
                "background_shares_endpoint": True,
            }
        }

        deleted = await client.delete(f"/models/endpoints/{endpoint_id}")
        assert deleted.status_code == 204
        assert (await client.get("/models/endpoints")).json() == []


async def test_embedding_rebind_triggers_reindex(monkeypatch):
    # Binding (or changing) the embedding role enqueues a background reindex, so a
    # model swap heals the stranded vectors without further operator action.
    from services.reindex import EmbeddingReindexer

    monkeypatch.setattr(embeddings, "probe_embedding", _passing_probe)
    triggered: list[str] = []
    monkeypatch.setattr(EmbeddingReindexer, "trigger", lambda self, owner: triggered.append(owner))

    async with client_app() as (client, _app):
        ep = (
            await client.post(
                "/models/endpoints",
                json={"name": "embed", "base_url": "http://x/v1", "model": "embed-m"},
            )
        ).json()
        put = await client.put(
            "/models/roles/embedding",
            json={"endpoint_ids": [ep["id"]], "model": "embed-m"},
        )
        assert put.status_code == 204
        assert triggered == ["operator"]  # the bind enqueued a reindex

        # An identical re-bind does not re-trigger (nothing changed).
        await client.put(
            "/models/roles/embedding",
            json={"endpoint_ids": [ep["id"]], "model": "embed-m"},
        )
        assert triggered == ["operator"]

        status = (await client.get("/models/embedding/reindex")).json()
        assert status["state"] == "idle"


async def test_rest_rejects_unknown_role_and_missing_endpoint():
    async with client_app() as (client, _app):
        bad_role = await client.put("/models/roles/nonsense", json={"endpoint_ids": []})
        assert bad_role.status_code == 422
        missing = await client.put("/models/roles/main", json={"endpoint_ids": ["nope"]})
        assert missing.status_code == 404


async def test_model_discovery_lists_provider_models(monkeypatch):
    async def fake_list(self, owner_id, endpoint_id):
        return ["gpt-4o", "gpt-4o-mini"]

    monkeypatch.setattr(ModelRegistry, "list_provider_models", fake_list)
    async with client_app() as (client, _app):
        ep = (
            await client.post(
                "/models/endpoints", json={"name": "local", "base_url": "http://x/v1"}
            )
        ).json()
        # The endpoint was created with no model — the picker discovers them.
        assert ep["model"] is None
        resp = await client.get(f"/models/endpoints/{ep['id']}/models")
        assert resp.status_code == 200
        assert resp.json() == {"models": ["gpt-4o", "gpt-4o-mini"], "supported": True}


async def test_model_discovery_degrades_when_provider_has_no_models_api(monkeypatch):
    async def fake_list(self, owner_id, endpoint_id):
        raise DegradedCapabilityError("no models API")

    monkeypatch.setattr(ModelRegistry, "list_provider_models", fake_list)
    async with client_app() as (client, _app):
        ep = (
            await client.post(
                "/models/endpoints", json={"name": "local", "base_url": "http://x/v1"}
            )
        ).json()
        resp = await client.get(f"/models/endpoints/{ep['id']}/models")
        assert resp.status_code == 200
        # Unsupported, not an error — the picker falls back to the configured model.
        assert resp.json() == {"models": [], "supported": False}


def test_extract_model_ids_handles_provider_shapes():
    from services.llm import _extract_model_ids

    # OpenAI / Anthropic: {"data": [{"id": …}]}
    assert _extract_model_ids({"data": [{"id": "b"}, {"id": "a"}]}) == ["a", "b"]
    # Gemini / Cohere / Ollama-native: {"models": [{"name": …}]}, "models/" stripped.
    assert _extract_model_ids({"models": [{"name": "models/gemini-1.5-pro"}]}) == ["gemini-1.5-pro"]
    # The models/ strip is scoped to the named-models shape — an OpenAI-shaped id
    # that legitimately starts with models/ is preserved.
    assert _extract_model_ids({"data": [{"id": "models/foo"}]}) == ["models/foo"]
    # A name that is only the prefix strips to empty and is dropped, not offered.
    assert _extract_model_ids({"models": [{"name": "models/"}]}) == []
    # Bare list of strings, de-duplicated.
    assert _extract_model_ids(["qwen", "llama3", "qwen"]) == ["llama3", "qwen"]
    # Recognized shape that lists nothing → empty (supported but empty).
    assert _extract_model_ids({"object": "list", "data": []}) == []
    # Unrecognized payload → None (no models API), distinct from empty.
    assert _extract_model_ids({"foo": "bar"}) is None


# --- endpoint health & disable -------------------------------------------


async def test_test_endpoint_categorizes_probe_failures(monkeypatch):
    """The connection test turns each probe outcome into a stable category + a
    plain-language detail that never carries the key — categorization is the backend's
    policy, not the frontend's."""
    reg = await _registry()
    ep = await reg.create_endpoint(
        OWNER, name="p", base_url="http://x/v1", model="m", api_key="super-secret"
    )
    req = httpx.Request("GET", "http://x/v1/models")

    # HTTP status → category: 404/405 (no models API) reads as healthy/reachable; 5xx is
    # a distinct server error; only an unexpected 4xx is "bad_response".
    for code, category in [
        (401, "auth"),
        (403, "auth"),
        (429, "rate_limited"),
        (404, "ok"),
        (405, "ok"),
        (500, "server_error"),
        (503, "server_error"),
        (418, "bad_response"),
    ]:

        async def _probe(base_url, api_key=None, *, client=None, _code=code):
            raise httpx.HTTPStatusError(
                "e", request=req, response=httpx.Response(_code, request=req)
            )

        monkeypatch.setattr(llm, "probe_openai_endpoint", _probe)
        health = await reg.test_endpoint(OWNER, ep.id)
        assert health.error_category == category, code
        assert health.status == ("ok" if category == "ok" else "error")
        assert "super-secret" not in health.error_detail

    # Transport failures: a connect timeout is unreachable (the host never answered); a
    # read timeout is a slow-but-alive provider; a bad body can't be understood.
    for exc, category in [
        (httpx.ConnectTimeout("slow"), "unreachable"),
        (httpx.ReadTimeout("slow"), "timeout"),
        (httpx.ConnectError("refused"), "unreachable"),
        (ValueError("not json"), "bad_response"),
    ]:

        async def _probe_exc(base_url, api_key=None, *, client=None, _exc=exc):
            raise _exc

        monkeypatch.setattr(llm, "probe_openai_endpoint", _probe_exc)
        health = await reg.test_endpoint(OWNER, ep.id)
        assert health.error_category == category
        assert health.status == "error"
        assert "super-secret" not in health.error_detail  # the key never leaks into the detail


async def test_test_endpoint_records_ok_and_persists(monkeypatch):
    reg = await _registry()
    ep = await reg.create_endpoint(OWNER, name="p", base_url="http://x/v1", model="m")

    async def _ok(base_url, api_key=None, *, client=None):
        return None

    monkeypatch.setattr(llm, "probe_openai_endpoint", _ok)
    health = await reg.test_endpoint(OWNER, ep.id)
    assert health.status == "ok" and health.error_category == "ok"
    # Persisted on the row so the catalog list shows health without re-probing per row.
    saved = await reg.get_endpoint(OWNER, ep.id)
    assert saved.last_status == "ok"
    assert saved.last_checked_at is not None


async def test_test_endpoint_unknown_is_not_found():
    reg = await _registry()
    with pytest.raises(NotFoundError):
        await reg.test_endpoint(OWNER, "does-not-exist")


async def test_disabled_endpoint_via_main_override_is_degraded():
    """A disabled endpoint chosen via the per-conversation main override is rejected,
    not silently run — the same invariant the role-chain path enforces."""
    reg = await _registry()
    ep = await reg.create_endpoint(OWNER, name="picked", base_url="http://p/v1", model="m")
    await reg.update_endpoint(OWNER, ep.id, enabled=False)
    with pytest.raises(DegradedCapabilityError):
        await _resolve(reg, "main", owner_id=OWNER, override_endpoint_id=ep.id, override_model="m")


async def test_disabled_endpoint_is_skipped_in_role_chain():
    """A disabled endpoint falls through to the next in the chain — the pre-emptive
    side of the runtime FallbackModel failover."""
    reg = await _registry()
    primary = await reg.create_endpoint(OWNER, name="a", base_url="http://a/v1", model="m1")
    backup = await reg.create_endpoint(OWNER, name="b", base_url="http://b/v1", model="m2")
    await reg.set_role(OWNER, "utility", [primary.id, backup.id])

    await reg.update_endpoint(OWNER, primary.id, enabled=False)
    model = await _resolve(reg, "utility", owner_id=OWNER)
    assert isinstance(model, OpenAIChatModel)
    assert not isinstance(model, FallbackModel)  # only the live backup remains
    assert model.model_name == "m2"


async def test_all_disabled_chain_is_degraded():
    reg = await _registry()
    ep = await reg.create_endpoint(OWNER, name="a", base_url="http://a/v1", model="m1")
    await reg.set_role(OWNER, "utility", [ep.id])
    await reg.update_endpoint(OWNER, ep.id, enabled=False)
    with pytest.raises(DegradedCapabilityError):
        await _resolve(reg, "utility", owner_id=OWNER)


async def test_endpoint_test_route_returns_verdict_and_reflects_on_list(monkeypatch):
    async def _ok(base_url, api_key=None, *, client=None):
        return None

    monkeypatch.setattr(llm, "probe_openai_endpoint", _ok)
    async with client_app() as (client, _app):
        ep = (
            await client.post(
                "/models/endpoints",
                json={"name": "p", "base_url": "http://x/v1", "model": "m"},
            )
        ).json()
        assert ep["enabled"] is True and ep["last_status"] is None
        resp = await client.post(f"/models/endpoints/{ep['id']}/test")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok" and body["error_category"] == "ok"
        assert "checked_at" in body
        # The verdict rides the endpoint listing for at-a-glance health.
        listed = (await client.get("/models/endpoints")).json()[0]
        assert listed["last_status"] == "ok"


async def test_endpoint_test_route_404_for_unknown():
    async with client_app() as (client, _app):
        assert (await client.post("/models/endpoints/nope/test")).status_code == 404


async def test_patch_endpoint_enabled_round_trips():
    async with client_app() as (client, _app):
        ep = (
            await client.post(
                "/models/endpoints",
                json={"name": "p", "base_url": "http://x/v1", "model": "m"},
            )
        ).json()
        assert ep["enabled"] is True
        patched = await client.patch(f"/models/endpoints/{ep['id']}", json={"enabled": False})
        assert patched.status_code == 200
        assert patched.json()["enabled"] is False


# ── Whether background work runs on the chat server ──────────────────────────────
#
# A label, not a decision — nothing branches on it. It exists because sharing one
# endpoint has a cost nothing else in the readout can show: a review, a title or a
# summary served by the same process evicts whatever the chat thread left in its prompt
# cache, and the operator's next turn pays to have the same prompt read again.


async def test_an_unbound_utility_reports_that_it_shares_the_chat_server():
    """The commonest shape, and the one where it is most true: `utility` unbound degrades
    onto `main` by the same rule titling and review already resolve through."""
    reg = await _registry()
    ep = await reg.create_endpoint(OWNER, name="local", base_url="http://x/v1", model="qwen")
    await reg.set_role(OWNER, "main", [ep.id])

    assert await reg.background_shares_main(OWNER) is True


async def test_a_second_row_on_the_same_server_shares_it_too():
    """Compared by URL rather than by endpoint id, because one local server is routinely
    configured twice — once per model — and two ids pointing at one process are one cache.
    The trailing version segment is normalised for the same reason: llama.cpp serves the
    chat route at the root and under `/v1`, so an operator can honestly configure either."""
    reg = await _registry()
    main = await reg.create_endpoint(OWNER, name="chat", base_url="http://x/v1", model="qwen-32b")
    twin = await reg.create_endpoint(OWNER, name="small", base_url="http://x/", model="qwen-4b")
    await reg.set_role(OWNER, "main", [main.id])
    await reg.set_role(OWNER, "utility", [twin.id])

    assert await reg.background_shares_main(OWNER) is True


async def test_a_second_process_on_another_port_does_not():
    """The fix this flag exists to point at — and the only one available on a server with
    no host-RAM prompt cache."""
    reg = await _registry()
    main = await reg.create_endpoint(OWNER, name="chat", base_url="http://x:8080/v1", model="big")
    other = await reg.create_endpoint(OWNER, name="util", base_url="http://x:8081/v1", model="sml")
    await reg.set_role(OWNER, "main", [main.id])
    await reg.set_role(OWNER, "utility", [other.id])

    assert await reg.background_shares_main(OWNER) is False


async def test_a_workspace_with_no_chat_model_reports_nothing_shared():
    """There is no chat prefix to evict, so the honest answer is False rather than the
    True an unbound `utility` would otherwise produce on its own."""
    reg = await _registry()
    assert await reg.background_shares_main(OWNER) is False


async def test_the_roles_listing_carries_the_fact_on_main():
    """On `main` rather than on `utility`: the listing returns *stored* bindings, so the
    unbound case has no `utility` row to carry a flag — and it is `main`'s prompt cache
    that a background call evicts either way."""
    async with client_app() as (client, _app):
        ep = (
            await client.post(
                "/models/endpoints",
                json={"name": "local", "base_url": "http://x/v1", "model": "m"},
            )
        ).json()
        await client.put("/models/roles/main", json={"endpoint_ids": [ep["id"]]})

        roles = (await client.get("/models/roles")).json()
        assert roles["main"]["background_shares_endpoint"] is True

        second = (
            await client.post(
                "/models/endpoints",
                json={"name": "util", "base_url": "http://y:9000/v1", "model": "m"},
            )
        ).json()
        await client.put("/models/roles/utility", json={"endpoint_ids": [second["id"]]})

        roles = (await client.get("/models/roles")).json()
        assert roles["main"]["background_shares_endpoint"] is False
