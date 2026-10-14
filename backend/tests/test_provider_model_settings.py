"""Standing per-provider model settings, and the Anthropic prompt cache they carry.

Two propositions, and the second is the reason the first exists.

The **placement**: settings a lab always wants are handed to the model where it is built
(``Model(settings=…)``), not to any one call site. Everything downstream — the fallback
chain, a turn parked for an approval and resumed later, every path that assembles its own
per-request settings — then carries them without knowing the lab exists, and a caller can
still override one because Pydantic AI merges a request's settings *over* the model's.

The **payload**: Anthropic's three cache breakpoints, which pay for the placement. A chat
turn re-sends its whole history behind an unchanging system block and a tool catalog of
some fourteen thousand tokens; without a breakpoint every turn buys them again.
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from typing import Any

from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models import Model
from pydantic_ai.models.anthropic import AnthropicModel, AnthropicModelSettings
from pydantic_ai.models.fallback import FallbackModel
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.profiles.openai import OpenAIModelProfile

import services.providers.anthropic as anthropic_provider
import services.providers.openai_compat as openai_compat
from services import llm
from services.providers import get_provider

CACHE_KEYS = ("anthropic_cache", "anthropic_cache_instructions", "anthropic_cache_tool_definitions")


def _openai_profile(model: Model) -> OpenAIModelProfile:
    return OpenAIModelProfile(**model.profile)


def spec(provider: str = "anthropic", **overrides) -> llm.EndpointSpec:
    return llm.EndpointSpec(
        base_url=overrides.pop("base_url", "https://api.anthropic.com"),
        model=overrides.pop("model", "claude-sonnet-4-5"),
        provider=provider,
        api_key="k",
        **overrides,
    )


# --- the placement ---------------------------------------------------------


class TestSettingsRideOnTheModel:
    async def test_a_models_own_settings_reach_the_request(self):
        """The mechanism the whole design rests on: settings given at construction arrive
        on every request without a single call site mentioning them."""
        seen: list[dict[str, Any]] = []

        def capture(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            seen.append(dict(info.model_settings or {}))
            return ModelResponse(parts=[TextPart("ok")])

        model = FunctionModel(
            capture, settings=AnthropicModelSettings(anthropic_cache="1h", temperature=0.1)
        )
        await Agent(model).run("hi")
        assert seen[0]["anthropic_cache"] == "1h"

    async def test_a_callers_setting_wins_over_the_models(self):
        """A standing default is a default, not a ceiling — otherwise the reasoning-off
        pass and the summarizer's own budgets could be silently overruled by a lab."""
        seen: list[dict[str, Any]] = []

        def capture(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            seen.append(dict(info.model_settings or {}))
            return ModelResponse(parts=[TextPart("ok")])

        model = FunctionModel(
            capture, settings=AnthropicModelSettings(temperature=0.1, anthropic_cache="5m")
        )
        await Agent(model).run("hi", model_settings={"temperature": 0.9})
        assert seen[0]["temperature"] == 0.9
        # …and the setting the caller said nothing about is still there.
        assert seen[0]["anthropic_cache"] == "5m"

    def test_a_fallback_chain_keeps_every_members_settings(self):
        """``FallbackModel`` delegates to the member it picks, so the standing settings
        have to survive the wrapping — a failover must not quietly stop caching."""
        chain = llm.build_chain([spec(), spec(base_url="https://proxy.example/v1")])
        assert isinstance(chain, FallbackModel)
        for member in chain.models:
            assert set(CACHE_KEYS) <= set(member.settings or {})


# --- the payload -----------------------------------------------------------


class TestTheAnthropicCacheBreakpoints:
    def test_the_built_model_carries_the_three_breakpoints(self):
        model = llm.build_model(spec())
        assert isinstance(model, AnthropicModel)
        settings = model.settings or {}
        assert all(settings.get(key) == "5m" for key in CACHE_KEYS)

    def test_the_conflicting_setting_is_never_sent(self):
        """``anthropic_cache_messages`` is the only one that cannot ride with the
        automatic breakpoint — the library raises on the pair — and it is a fallback for
        gateways that reject the top-level parameter, not a fourth breakpoint."""
        assert "anthropic_cache_messages" not in (llm.build_model(spec()).settings or {})

    def test_the_library_reads_the_trio_as_a_live_cache_request(self):
        """Asserted through Pydantic AI's own resolution rather than against our dict, so
        the day the library renames or re-scopes a key this fails instead of silently
        sending three settings nothing reads."""
        model = llm.build_model(spec())
        assert model.resolve_prompt_cache_retention(None) == timedelta(minutes=5)

    def test_the_ttl_comes_from_the_operators_configuration(self, monkeypatch):
        monkeypatch.setattr(
            anthropic_provider, "get_settings", lambda: SimpleNamespace(anthropic_cache_ttl="1h")
        )
        settings = llm.build_model(spec()).settings or {}
        assert all(settings.get(key) == "1h" for key in CACHE_KEYS)

    def test_off_sends_no_breakpoints_at_all(self, monkeypatch):
        """The escape hatch for an Anthropic-compatible proxy that rejects
        ``cache_control``: the whole trio has to be *absent*, not set to a disabled-looking
        value, since anything the adapter still declares is something the proxy still
        refuses."""
        monkeypatch.setattr(
            anthropic_provider, "get_settings", lambda: SimpleNamespace(anthropic_cache_ttl="off")
        )
        model = llm.build_model(spec())
        assert not model.settings
        # …and the library agrees there is no cache request to honor, read through its own
        # resolution rather than our dict.
        assert model.resolve_prompt_cache_retention(None) is None

    def test_the_default_ttl_is_the_messages_apis_own(self):
        """The operator's lever defaults to the tier the Messages API itself uses, so an
        installation that never touches it caches exactly as Anthropic intends."""
        settings = llm.build_model(spec()).settings
        assert settings is not None
        assert all(settings.get(key) == "5m" for key in CACHE_KEYS)


# --- the hook itself -------------------------------------------------------


class TestTheProviderHook:
    def test_a_provider_that_declares_nothing_builds_a_bare_model(self):
        """The default is ``{}``, and ``{}`` must reach the model as "no standing
        settings" — an empty dict here would still be a dict every merge has to walk."""
        for provider_id, base_url in (
            ("openai-compatible", "https://local.example/v1"),
            ("google", "https://generativelanguage.googleapis.com"),
        ):
            model = llm.build_model(spec(provider_id, base_url=base_url, model="m"))
            assert not model.settings, provider_id

    def test_the_default_hook_answers_for_every_adapter(self):
        """Read through the protocol's default rather than each adapter's own method:
        only Anthropic declares one, and the other two must still be answerable."""
        from services.providers.base import Provider

        descriptor = llm.descriptor_of(spec())
        assert Provider.model_settings(get_provider("openai-compatible"), descriptor) == {}
        assert set(get_provider("anthropic").model_settings(descriptor)) == set(CACHE_KEYS)

    def test_the_descriptor_is_projected_from_the_spec(self):
        """One projection for the reasoning-off lookup and the settings hook, so the two
        cannot end up asking about different models."""
        descriptor = llm.descriptor_of(
            spec("openai-compatible", base_url="https://x/v1", model="qwen3:8b", thinking=True)
        )
        assert (descriptor.model_id, descriptor.base_url, descriptor.thinking) == (
            "qwen3:8b",
            "https://x/v1",
            True,
        )


# --- the OpenAI-compatible adapter's one cache field -----------------------


def local(**overrides) -> llm.EndpointSpec:
    overrides.setdefault("base_url", "http://localhost:8080/v1")
    return spec("openai-compatible", model="qwen3-32b", **overrides)


def _retention(value: str):
    return lambda: SimpleNamespace(openai_cache_retention=value)


class TestTheOpenAICompatibleCacheField:
    """This adapter fronts *every* OpenAI-shaped server — llama.cpp, vLLM, LM Studio, MLX, a
    hosted gateway, a non-US lab — and the only thing it knows about what is behind the base
    URL is the model name the operator typed. So what it may send is narrow on purpose, and
    the narrowness is what these pin.
    """

    def test_a_local_endpoint_sends_no_cache_settings_by_default(self):
        """Byte-identical to what shipped before the field existed. The within-session case
        is already covered by OpenAI's own implicit retention, and the overwhelming majority
        of endpoints behind this adapter have never heard of the field."""
        model = llm.build_model(local())
        assert not model.settings
        assert model.resolve_prompt_cache_retention(None) is None

    def test_retention_comes_from_the_operators_configuration(self, monkeypatch):
        monkeypatch.setattr(openai_compat, "get_settings", _retention("24h"))
        model = llm.build_model(local())
        assert model.settings == {"openai_prompt_cache_retention": "24h"}
        # Read through the library's own resolution rather than our dict, so this fails if
        # the field stops meaning what we think it means.
        assert model.resolve_prompt_cache_retention(None) == timedelta(hours=24)

    def test_off_also_strips_a_cache_setting_a_caller_supplies(self, monkeypatch):
        """The escape hatch has to close the door, not merely stop this adapter opening it.
        An operator switches retention off because their server rejects unknown fields, and a
        per-request setting from anywhere else would reach it just the same."""
        monkeypatch.setattr(openai_compat, "get_settings", _retention("off"))
        profile = _openai_profile(llm.build_model(local()))
        assert "openai_unsupported_model_settings" in profile
        assert set(profile["openai_unsupported_model_settings"]) == {
            "openai_prompt_cache_key",
            "openai_prompt_cache_options",
            "openai_prompt_cache_retention",
        }

    def test_retention_on_declares_nothing_unsupported(self, monkeypatch):
        monkeypatch.setattr(openai_compat, "get_settings", _retention("in_memory"))
        profile = _openai_profile(llm.build_model(local()))
        assert "openai_unsupported_model_settings" in profile
        assert not profile["openai_unsupported_model_settings"]

    def test_this_adapter_never_sends_a_cache_key_or_breakpoints(self, monkeypatch):
        """Pinned with the reason, so a contributor adding either has to argue with it.

        `openai_prompt_cache_key` selects a cache *shard*; it never decides whether a prefix
        is cached, is worth nothing on every local engine, and there is no conversation
        visible at this seam to key on. `openai_prompt_cache_options` (and `CachePoint`) are
        meaningful on two hosted model families recognisable here only by **name** — so a
        local model an operator happened to name after one of them would be handed
        breakpoints it cannot parse. That inference is the bug this adapter's boundary exists
        to prevent; explicit breakpoints would argue for a distinct native adapter.
        """
        for value in ("off", "in_memory", "24h"):
            monkeypatch.setattr(openai_compat, "get_settings", _retention(value))
            sent = set(llm.build_model(local()).settings or {})
            assert "openai_prompt_cache_key" not in sent
            assert "openai_prompt_cache_options" not in sent

    def test_the_merged_system_messages_profile_survives(self, monkeypatch):
        """Untouched by any of this, and it must be: merging the leading system messages into
        one block is both what keeps a Qwen-family chat template from refusing the request and
        good for caching."""
        for value in ("off", "24h"):
            monkeypatch.setattr(openai_compat, "get_settings", _retention(value))
            profile = _openai_profile(llm.build_model(local()))
            assert "openai_chat_supports_multiple_system_messages" in profile
            assert profile["openai_chat_supports_multiple_system_messages"] is False

    def test_a_fallback_chain_keeps_the_setting_on_every_member(self, monkeypatch):
        """The reason it is applied at construction: a failover must not silently drop to a
        request shape the operator did not choose."""
        monkeypatch.setattr(openai_compat, "get_settings", _retention("24h"))
        chain = llm.build_chain([local(), local(base_url="http://localhost:8081/v1")])
        assert isinstance(chain, FallbackModel)
        assert all(
            member.settings == {"openai_prompt_cache_retention": "24h"} for member in chain.models
        )
