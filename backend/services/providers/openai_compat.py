"""The universal adapter — any server speaking the OpenAI wire protocol.

The default for every endpoint (and the shape every local engine — vLLM, LM Studio,
llama.cpp, MLX servers — speaks). Reasoning-off falls back to the model-name
heuristics in ``services/reasoning``, because a generic gateway can front any model
family.
"""

from __future__ import annotations

from pydantic_ai.models import Model
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.profiles.openai import OpenAIModelProfile
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.settings import ModelSettings

from core.config import get_settings
from services import llm, reasoning
from services.llm import EndpointSpec
from services.providers.base import ModelLimits, ProviderPreset

#: The three OpenAI cache fields. Named as a set so that switching retention off declares
#: all of them unsupported rather than only the one this adapter would otherwise have sent —
#: the operator's escape hatch is "this server rejects unknown fields", and that is a claim
#: about all three.
_CACHE_SETTINGS = (
    "openai_prompt_cache_key",
    "openai_prompt_cache_options",
    "openai_prompt_cache_retention",
)


class OpenAICompatProvider:
    id = "openai-compatible"
    display_name = "OpenAI-compatible"
    # A key is the server's choice, not the protocol's — a local engine ignores auth,
    # a hosted gateway demands it. The operator decides; nothing is validated away.
    requires_key = False
    preset = ProviderPreset(
        default_base_url=None,
        key_hint="sk-… (only if the server requires one)",
        docs_url=None,
    )

    # We send a standing prompt *and* per-turn instructions, which the OpenAI wire
    # format carries as two system messages at the head. Hosted APIs accept that; a
    # local engine hands the messages to the model's own chat template, and the Qwen
    # family's (among others) raises "System message must be at the beginning." on
    # any system message that is not the first. Declaring the endpoint strict makes
    # Pydantic AI merge the leading system messages into one at wire-prep — the
    # instructions still come from the live agent, never from history, so nothing
    # about their authority changes. Harmless where multiples were fine.
    def build_model(self, spec: EndpointSpec) -> Model:
        # The OpenAI client refuses a None key outright, so a keyless local server
        # gets a placeholder — an auth header the server ignores. The placeholder is
        # an adapter-boundary quirk, never a value other layers see or store.
        provider = OpenAIProvider(base_url=spec.base_url, api_key=spec.api_key or "unused")
        # A partial profile: merged over whatever the model name resolves to, so this
        # overrides two fields and leaves every inferred capability intact. The window is
        # the endpoint's own, never the model's advertised maximum — see `Provider
        # .build_model`, and note this adapter is where the two diverge most, since any
        # server at all can be behind this base URL.
        settings = self.model_settings(llm.descriptor_of(spec))
        profile = OpenAIModelProfile(
            openai_chat_supports_multiple_system_messages=False,
            context_window=spec.context_window,
            # With retention switched off, declare the three cache fields unsupported so the
            # library strips one a caller supplies per request. The point is not tidiness: a
            # local engine handed an unknown field may reject the whole request, and the
            # operator's own escape hatch has to actually close the door rather than only
            # stop this adapter from opening it.
            openai_unsupported_model_settings=() if settings else _CACHE_SETTINGS,
        )
        # At construction, not per call, so it survives a `FallbackModel` chain and a turn
        # parked for approval and resumed hours later. The library merges a request's own
        # settings over the model's, so a caller can still override deliberately.
        return OpenAIChatModel(spec.model, provider=provider, profile=profile, settings=settings)

    def model_settings(self, descriptor: reasoning.ModelDescriptor) -> ModelSettings:
        """Prompt-cache retention, and nothing else — usually not even that.

        OpenAI caches a matching prefix **implicitly**, with no request field: there is one
        breakpoint, it needs at least a thousand-odd tokens, and the whole rendered prefix
        (tools included) has to match byte for byte. So the work that makes caching pay on
        this wire is *prefix stability*, which is structural and lives nowhere near here. The
        only thing a request field can add is holding that prefix longer than the default
        five to ten minutes, which is what `openai_cache_retention` asks for.

        Nothing here reads ``descriptor``, and that is the load-bearing part. This adapter
        fronts **every** OpenAI-shaped server — llama.cpp, vLLM, LM Studio, MLX, a hosted
        gateway, a non-US lab — and the only thing it knows about what is behind the base URL
        is the model *name* the operator typed. So:

        - **``openai_prompt_cache_key`` is never sent.** It selects a cache shard; it does not
          decide whether a prefix is cached. Worth nothing on every local engine, and there is
          no conversation visible at this seam to key on anyway.
        - **``openai_prompt_cache_options`` (and ``CachePoint``) are never sent.** They are
          meaningful on two hosted families, recognisable here only by name — and a local
          model an operator happened to name after one of them would be handed breakpoints it
          cannot parse. That inference is exactly the bug this adapter's boundary exists to
          prevent. If explicit breakpoints ever matter, the answer is a distinct native
          adapter, not a name match.

        Default off: the within-session case is already covered by OpenAI's own retention, so
        the field earns its keep only for a thread an operator comes back to tomorrow.
        """
        retention = get_settings().openai_cache_retention
        if retention == "off":
            return {}
        return {"openai_prompt_cache_retention": retention}

    # Reached through the module (not from-imports) so a test that monkeypatches
    # `services.llm` still intercepts the adapter's calls.
    async def discover(self, base_url: str, api_key: str | None, *, client=None) -> list[str]:
        return await llm.discover_openai_models(base_url, api_key, client=client)

    async def probe(self, base_url: str, api_key: str | None, *, client=None) -> None:
        await llm.probe_openai_endpoint(base_url, api_key, client=client)

    async def context_window(
        self, base_url: str, api_key: str | None, model: str, *, client=None
    ) -> int | None:
        return await llm.discover_openai_context_window(base_url, model, api_key, client=client)

    async def model_limits(
        self, base_url: str, api_key: str | None, model: str, *, client=None
    ) -> ModelLimits:
        # No output ceiling: this wire treats an absent `max_tokens` as the server's own
        # limit, so there is no library default to replace.
        window, slots = await llm.discover_openai_limits(base_url, model, api_key, client=client)
        return ModelLimits(context_window=window, parallel_slots=slots)

    def reasoning_off(self, descriptor: reasoning.ModelDescriptor) -> ModelSettings:
        return reasoning.disable_thinking(descriptor)


PROVIDER = OpenAICompatProvider()
