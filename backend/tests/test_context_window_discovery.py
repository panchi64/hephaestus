"""Where a model's context window comes from.

The window is the number every limit-keeping mechanism measures against — the gauge
under the composer, auto-compaction's trigger, the overflow warning — so a thread whose
window is unknown has no working guard at all. It used to be a field the operator typed
in and usually didn't; it is now asked of the provider, with the field kept as the
override for providers that won't say.

Two rules carry the design and both are asserted here: an operator-set value always
wins over a discovered one, and a window that can't be established stops the turn
rather than letting it run unguarded.
"""

from __future__ import annotations

from dataclasses import replace
from typing import override

import httpx
import pytest
from pydantic_ai.settings import ModelSettings

from core.vault import Vault
from runs import Endpoint
from services import llm
from services.llm import EndpointSpec
from services.providers import get_provider
from services.providers.anthropic import PROVIDER as ANTHROPIC
from services.providers.base import ModelLimits
from services.providers.google import PROVIDER as GOOGLE
from services.providers.openai_compat import OpenAICompatProvider
from services.reasoning import ModelDescriptor


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# ── OpenAI-wire servers: no standard field, so several are read ──────────────────


@pytest.mark.parametrize(
    "key",
    ["context_length", "max_context_length", "max_model_len", "context_window", "n_ctx"],
)
async def test_reads_whichever_context_key_the_server_invented(key):
    # "OpenAI-compatible" agrees on the chat route and nothing else: the OpenAI
    # /v1/models schema has no context field at all, so every server that reports one
    # made up its own name. vLLM says max_model_len, llama.cpp n_ctx, and so on.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"id": "m", key: 4096}]})

    async with _client(handler) as client:
        assert (
            await llm.discover_openai_context_window("http://server/v1", "m", client=client) == 4096
        )


async def test_falls_back_to_lm_studios_own_listing():
    # The most common local setup can't answer on the OpenAI route — LM Studio reports
    # no context there — but does on its native one. Without this fallback, discovery
    # would come back empty for the very endpoint it most needs to serve.
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "qwen"}]})
        if request.url.path.endswith("/props"):
            return httpx.Response(404)  # not llama.cpp
        assert request.url.path == "/api/v0/models"
        return httpx.Response(200, json={"data": [{"id": "qwen", "max_context_length": 262144}]})

    async with _client(handler) as client:
        assert (
            await llm.discover_openai_context_window("http://server/v1", "qwen", client=client)
            == 262144
        )


async def test_prefers_the_length_actually_loaded():
    # A model loaded at 32k in a server that *could* do 256k has a real ceiling of 32k.
    # The gauge has to measure the limit the next turn will hit, not the one the
    # hardware would allow.
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "qwen"}]})
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": "qwen", "max_context_length": 262144, "loaded_context_length": 32768}
                ]
            },
        )

    async with _client(handler) as client:
        assert (
            await llm.discover_openai_context_window("http://server/v1", "qwen", client=client)
            == 32768
        )


async def test_llama_cpp_answers_on_its_own_status_route():
    # The other half of the local story. llama.cpp's /v1/models row is keyed by a file
    # path or an alias that rarely equals the model name the operator bound, and carries
    # no context field anyway — so the listing probes come back empty on the server whose
    # window is least likely to be the model's default. Its own /props reports the
    # context the process was actually started with.
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/props":
            return httpx.Response(
                200,
                json={
                    "default_generation_settings": {"n_ctx": 16384, "n_predict": -1},
                    "total_slots": 1,
                },
            )
        return httpx.Response(200, json={"data": [{"id": "/models/qwen.gguf"}]})

    async with _client(handler) as client:
        assert (
            await llm.discover_openai_context_window("http://server/v1", "qwen", client=client)
            == 16384
        )


async def test_props_is_tried_under_the_base_url_when_there_is_no_version_segment():
    # An operator can configure the server root rather than /v1 (llama.cpp serves the
    # chat route at both). The probe has to follow the base it was given, or the most
    # forgiving configuration would be the one that fails.
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == "/props":
            return httpx.Response(200, json={"default_generation_settings": {"n_ctx": 8192}})
        return httpx.Response(200, json={"data": []})

    async with _client(handler) as client:
        assert await llm.discover_openai_context_window("http://s", "m", client=client) == 8192
    assert seen.count("/props") == 1  # not probed twice for one candidate URL


async def test_a_loaded_window_beats_a_maximum_found_by_an_earlier_probe():
    # The preference rule, across probes rather than within one row. A server that
    # advertises what it *could* run (256k) while running 16k would otherwise leave every
    # guard measuring against a window no request will ever be allowed to reach.
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/props":
            return httpx.Response(200, json={"default_generation_settings": {"n_ctx": 16384}})
        return httpx.Response(200, json={"data": [{"id": "qwen", "context_length": 262144}]})

    async with _client(handler) as client:
        assert (
            await llm.discover_openai_context_window("http://server/v1", "qwen", client=client)
            == 16384
        )


async def test_a_maximum_still_stands_when_nothing_reports_a_loaded_window():
    # And the fallback: a hosted gateway states a maximum and has no /props at all. The
    # extra probe must cost the answer nothing.
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/props"):
            return httpx.Response(404)
        return httpx.Response(200, json={"data": [{"id": "gpt-x", "context_length": 128000}]})

    async with _client(handler) as client:
        assert (
            await llm.discover_openai_context_window("http://gw/v1", "gpt-x", client=client)
            == 128000
        )


async def test_a_props_body_that_says_nothing_useful_is_ignored():
    # Never-raise, including on a route that answered 200 with something else entirely —
    # /props is a common enough path that another server could own it.
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/props"):
            return httpx.Response(200, json={"default_generation_settings": {"n_ctx": 0}})
        return httpx.Response(200, json={"data": [{"id": "m"}]})

    async with _client(handler) as client:
        assert await llm.discover_openai_context_window("http://s/v1", "m", client=client) is None


async def test_only_the_asked_for_model_answers():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"data": [{"id": "small", "n_ctx": 8192}, {"id": "big", "n_ctx": 131072}]},
        )

    async with _client(handler) as client:
        assert (
            await llm.discover_openai_context_window("http://s/v1", "big", client=client) == 131072
        )


async def test_a_server_that_says_nothing_yields_none_rather_than_raising():
    # Every caller's response to "couldn't establish it" is identical, so the failure
    # is collapsed here instead of at each call site.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"id": "m"}]})

    async with _client(handler) as client:
        assert await llm.discover_openai_context_window("http://s/v1", "m", client=client) is None


async def test_an_unreachable_server_is_not_an_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    async with _client(handler) as client:
        assert await llm.discover_openai_context_window("http://s/v1", "m", client=client) is None


async def test_a_nonsense_value_is_ignored():
    # `True` is an `int` subclass — a server answering `"n_ctx": true` would otherwise
    # produce a one-token context window, and a gauge pinned at 100% forever.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"id": "m", "n_ctx": True, "max_model_len": 0}]})

    async with _client(handler) as client:
        assert await llm.discover_openai_context_window("http://s/v1", "m", client=client) is None


# ── The providers that state it, and the one that doesn't ────────────────────────


async def test_gemini_reports_its_input_limit():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "models": [
                    {"name": "models/gemini-x", "inputTokenLimit": 1048576},
                    {"name": "models/other", "inputTokenLimit": 32768},
                ]
            },
        )

    async with _client(handler) as client:
        assert await GOOGLE.context_window("https://g", "key", "gemini-x", client=client) == 1048576


async def test_anthropic_reads_both_limits_off_one_models_entry():
    # Discovered rather than tabled: a table of known Anthropic windows is correct until
    # a new model or an extended-context beta makes it silently wrong. One request
    # answers both, since both sit on the same entry.
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        assert request.headers["x-api-key"] == "key"
        return httpx.Response(
            200,
            json={"id": "claude-x", "max_input_tokens": 1_000_000, "max_tokens": 128_000},
        )

    async with _client(handler) as client:
        limits = await ANTHROPIC.model_limits("https://a", "key", "claude-x", client=client)
        window = await ANTHROPIC.context_window("https://a/v1", "key", "claude-x", client=client)
    assert limits == ModelLimits(context_window=1_000_000, max_output_tokens=128_000)
    assert window == 1_000_000
    assert seen == ["/v1/models/claude-x", "/v1/models/claude-x"]


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(404, json={"type": "error"}),
        httpx.Response(200, json={"id": "claude-x"}),
        httpx.Response(200, json={"id": "claude-x", "max_input_tokens": True, "max_tokens": 0}),
    ],
)
async def test_anthropic_answers_none_where_the_entry_does_not_say(response):
    # An Anthropic-compatible proxy with no such route, or an entry without the fields,
    # leaves both unknown — and the library's own figures in place — rather than raising.
    async with _client(lambda request: response) as client:
        limits = await ANTHROPIC.model_limits("https://a", "key", "claude-x", client=client)
    assert limits == ModelLimits()


async def test_anthropic_unreachable_is_not_an_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    async with _client(handler) as client:
        assert await ANTHROPIC.model_limits("https://a", "k", "claude-x", client=client) == (
            ModelLimits()
        )


# ── Resolution: precedence, caching, and the gate ────────────────────────────────


async def _registry(tmp_path, monkeypatch, *, window: int | None, max_output: int | None = None):
    """A registry whose stub provider reports ``window`` and ``max_output``."""
    from pydantic_ai.models.test import TestModel

    from core.db import init_db, make_engine
    from services import providers
    from services.registry import ModelRegistry

    class _Stub(OpenAICompatProvider):
        id = "probe-stub"
        display_name = "Probe stub"
        requires_key = False
        asked = 0

        @override
        async def model_limits(
            self,
            base_url: str,
            api_key: str | None,
            model: str,
            *,
            client: object = None,
        ) -> ModelLimits:
            type(self).asked += 1
            return ModelLimits(context_window=window, max_output_tokens=max_output)

        # Enough of the rest of the contract for a full resolve to run: the gate test
        # goes through `resolve_detailed`, which builds a model and asks the adapter
        # how to quiet its reasoning before it ever reaches the window check.
        @override
        def build_model(self, spec: EndpointSpec) -> TestModel:
            return TestModel(custom_output_text="ok")

        @override
        def reasoning_off(self, descriptor: ModelDescriptor) -> ModelSettings:
            return {}

    _Stub.asked = 0
    registry_map = dict(providers._registry())
    registry_map["probe-stub"] = _Stub()
    monkeypatch.setattr(providers, "_PROVIDERS", registry_map)

    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    vault = Vault(tmp_path / "keyfile.json")
    await vault.setup("pw")
    return ModelRegistry(engine, vault), _Stub


async def _bind(registry, *, context_window: int | None):
    endpoint = await registry.create_endpoint(
        "operator",
        name="stub",
        base_url="http://stub/v1",
        provider="probe-stub",
        model="stub-model",
        context_window=context_window,
    )
    await registry.set_role("operator", "main", [endpoint.id])
    return endpoint


async def test_a_discovered_window_reaches_resolution(tmp_path, monkeypatch):
    registry, _ = await _registry(tmp_path, monkeypatch, window=200_000)
    await _bind(registry, context_window=None)
    assert await registry.main_context_window("operator") == 200_000


async def test_the_operators_own_value_wins(tmp_path, monkeypatch):
    # The field is the override for providers that can't answer, so a discovered value
    # silently replacing a deliberate one would make the setting appear not to work.
    registry, _ = await _registry(tmp_path, monkeypatch, window=200_000)
    await _bind(registry, context_window=8192)
    assert await registry.main_context_window("operator") == 8192


async def test_the_output_ceiling_is_discovered_even_beside_an_operator_window(
    tmp_path, monkeypatch
):
    # The operator's field overrides the window only; the output ceiling has no field of
    # its own, so the provider is still asked for it, once, and remembered.
    registry, stub = await _registry(tmp_path, monkeypatch, window=200_000, max_output=64_000)
    await _bind(registry, context_window=8192)
    for _ in range(3):
        [spec] = await registry._resolve_specs("main", owner_id="operator")
        assert (spec.context_window, spec.max_output_tokens) == (8192, 64_000)
    assert stub.asked == 1


async def test_the_provider_is_asked_once_and_then_remembered(tmp_path, monkeypatch):
    # This sits on the path of every turn; an uncached lookup would put a provider
    # round-trip in front of each one for a number already known.
    registry, stub = await _registry(tmp_path, monkeypatch, window=200_000)
    await _bind(registry, context_window=None)
    for _ in range(3):
        assert await registry.main_context_window("operator") == 200_000
    assert stub.asked == 1


async def test_editing_an_endpoint_re_asks(tmp_path, monkeypatch):
    # A write can move the base URL, the model or the operator's own value — every
    # input the memo keyed on — so it drops rather than being reasoned about.
    registry, stub = await _registry(tmp_path, monkeypatch, window=200_000)
    endpoint = await _bind(registry, context_window=None)
    await registry.main_context_window("operator")
    await registry.update_endpoint("operator", endpoint.id, name="renamed")
    await registry.main_context_window("operator")
    assert stub.asked == 2


async def test_a_provider_that_says_nothing_leaves_the_window_unknown(tmp_path, monkeypatch):
    registry, _ = await _registry(tmp_path, monkeypatch, window=None)
    await _bind(registry, context_window=None)
    assert await registry.main_context_window("operator") is None


async def test_a_turn_is_refused_when_no_window_can_be_established(tmp_path, monkeypatch):
    """The gate. Without a window every guard that keeps a thread inside its limit is
    inert, so the turn stops here — with a message naming the fix — instead of running
    normally until the provider rejects it outright."""
    from fastapi import HTTPException

    from routes.chat import resolve_turn_models

    registry, _ = await _registry(tmp_path, monkeypatch, window=None)
    await _bind(registry, context_window=None)

    with pytest.raises(HTTPException) as caught:
        await resolve_turn_models(registry, None, None, owner_id="operator")
    assert caught.value.status_code == 422
    assert "context window" in caught.value.detail


# ── The binding: what the frontend reads to know a window exists ─────────────────


async def test_the_window_follows_the_binding_not_the_endpoint_row(tmp_path, monkeypatch):
    """The bug the send gate shipped with. An endpoint row carries only a *default*
    model and usually doesn't set one — the model in play is the one the role pinned. A
    window read off the endpoint alone therefore answers null on exactly this workspace's
    shape (one server, many models, the choice made in the picker), which had the
    composer refusing to send on a perfectly configured thread."""
    registry, _ = await _registry(tmp_path, monkeypatch, window=262_144)
    endpoint = await registry.create_endpoint(
        "operator", name="stub", base_url="http://stub/v1", provider="probe-stub"
    )
    # No default model on the row — the binding names it, exactly as the picker leaves it.
    assert endpoint.model is None
    await registry.set_role("operator", "main", [endpoint.id], model="stub-model")

    assert await registry.role_context_window("operator", "main") == 262_144


async def test_every_role_reports_its_own_window(tmp_path, monkeypatch):
    """The field means the same thing on every row of the roles listing — a value
    populated for one role and silently null for the others is a trap for the next
    caller."""
    registry, _ = await _registry(tmp_path, monkeypatch, window=200_000)
    await _bind(registry, context_window=None)
    assert await registry.role_context_window("operator", "main") == 200_000
    # Unconfigured roles answer null rather than raising: this is a read path.
    assert await registry.role_context_window("operator", "utility") is None


async def test_main_context_window_is_the_same_answer(tmp_path, monkeypatch):
    """The gate calls one, the roles listing the other. If they could disagree, the
    operator would meet a refusal from a composer that looked ready — or be blocked on a
    thread that would have run fine."""
    registry, _ = await _registry(tmp_path, monkeypatch, window=262_144)
    await _bind(registry, context_window=None)
    assert await registry.main_context_window("operator") == await registry.role_context_window(
        "operator", "main"
    )


async def test_an_unresolvable_role_reports_no_window_rather_than_raising(tmp_path, monkeypatch):
    registry, _ = await _registry(tmp_path, monkeypatch, window=None)
    await _bind(registry, context_window=None)
    assert await registry.role_context_window("operator", "main") is None


# ── What the same /props response says about prompt caching ──────────────────────
#
# Diagnostic, not a decision: nothing branches on any of this. It is read because the
# magnitude of one cause of a slow local turn — a background call on the shared endpoint
# evicting the chat thread's cached prefix — depends entirely on the build the operator is
# running, and that is the one input the codebase cannot reason its way to. Read off the
# response the window probe already fetched, so it costs no extra request.


async def test_the_props_response_also_yields_the_servers_cache_posture():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/props"
        return httpx.Response(
            200,
            json={
                "default_generation_settings": {"n_ctx": 32768, "cache_reuse": 256},
                "total_slots": 4,
                "build_info": "b7412-abcdef1",
                "cache_ram": -1,
            },
        )

    async with _client(handler) as client:
        posture = await llm._llama_cpp_props("http://s", None, client=client)

    assert posture is not None
    assert posture.context_window == 32768
    assert posture.total_slots == 4
    assert posture.build_info == "b7412-abcdef1"
    # Read from the top level *and* from the generation settings, because which of the two
    # carries a given knob has moved between builds and neither placement is promised.
    assert posture.cache_settings == {"cache_ram": -1, "cache_reuse": 256}
    assert "cache_ram=-1" in posture.summary()


async def test_a_llama_cpp_servers_slot_count_becomes_its_endpoints_run_capacity():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/props":
            return httpx.Response(
                200, json={"default_generation_settings": {"n_ctx": 8192}, "total_slots": 1}
            )
        return httpx.Response(404)

    async with _client(handler) as client:
        limits = await get_provider("openai-compatible").model_limits(
            "http://s/v1", None, "m", client=client
        )

    assert limits == ModelLimits(context_window=8192, parallel_slots=1)
    spec = llm.EndpointSpec(
        base_url="http://s/v1", model="m", endpoint_id="ep", name="local", parallel_slots=1
    )
    assert spec.gate_endpoint() == Endpoint(id="ep", name="local", capacity=1)
    assert replace(spec, max_concurrent_runs=3).gate_endpoint() == Endpoint(
        id="ep", name="local", capacity=3
    )


async def test_a_server_that_mentions_no_cache_settings_says_so_rather_than_guessing():
    """Absent has to read as "did not say". A default filled in here would be a claim about
    a build nobody looked at — which is exactly the guess this probe exists to replace."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"default_generation_settings": {"n_ctx": 8192}})

    async with _client(handler) as client:
        posture = await llm._llama_cpp_props("http://s", None, client=client)

    assert posture is not None
    assert posture.cache_settings == {}
    assert posture.total_slots is None
    assert posture.build_info is None
    assert "no cache settings reported" in posture.summary()


async def test_a_server_without_props_still_reports_its_backend():
    """Written because the probe was **inert on the most common local setup**. LM Studio
    answers `/props` with an "unexpected endpoint" error and its OpenAI-shaped completions
    carry no `timings` block — measured against a live one — yet its own listing states which
    runtime is serving, which is what says whose tuning knobs apply.

    Both runtimes were measured reusing a matching prefix, so this is *not* a claim that one
    caches and the other does not. What it distinguishes is that the `/props` flags exist only
    for a real `llama-server`; behind an embedded runtime the only lever is binding `utility`
    to a second endpoint."""
    row = {
        "id": "muse-glimmer-30b",
        "compatibility_type": "gguf",
        "max_context_length": 131072,
        "state": "not-loaded",
    }

    posture = llm._native_posture(row)

    assert posture is not None
    assert posture.backend == "gguf"
    assert posture.context_window == 131072
    assert "backend=gguf" in posture.summary()
    # And the llama.cpp-only facts stay absent rather than being guessed: a slot count and
    # the cache flags are a `llama-server` command line, and LM Studio does not run one.
    assert posture.total_slots is None
    assert posture.cache_settings == {}


async def test_an_mlx_row_is_reported_as_mlx():
    posture = llm._native_posture({"id": "m", "compatibility_type": "mlx"})
    assert posture is not None
    assert posture.backend == "mlx"


async def test_a_row_that_names_nothing_yields_no_posture():
    """Absent reads as "did not say", as everywhere else here — a posture with no backend and
    no window is a row of question marks in the log, which is worse than no row."""
    assert llm._native_posture({"id": "m", "object": "model"}) is None
    assert llm._native_posture(None) is None


async def test_the_posture_probe_never_changes_the_window_that_is_returned():
    """The window is load-bearing — every guard measures against it — and the posture is
    not. So the probe is additive by construction: same response, same answer."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/props":
            return httpx.Response(
                200,
                json={
                    "default_generation_settings": {"n_ctx": 16384},
                    "total_slots": 1,
                    "build_info": "b1-old",
                },
            )
        return httpx.Response(200, json={"data": []})

    async with _client(handler) as client:
        assert await llm.discover_openai_context_window("http://s/v1", "m", client=client) == 16384
