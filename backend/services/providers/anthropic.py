"""Anthropic — native adapter over Pydantic AI's ``AnthropicModel``.

Speaks the Messages API directly (system prompts, tool use, and thinking blocks in
their native shape) instead of squeezing Claude through an OpenAI-compat gateway.
``base_url`` is honored, so an Anthropic-compatible proxy still works.
"""

from __future__ import annotations

from typing import Literal, override
from urllib.parse import quote, urlparse

import httpx
from pydantic import JsonValue
from pydantic_ai.models import Model
from pydantic_ai.models.anthropic import AnthropicModel, AnthropicModelSettings
from pydantic_ai.profiles import ModelProfile
from pydantic_ai.providers.anthropic import AnthropicProvider as _SdkProvider
from pydantic_ai.settings import ModelSettings, ThinkingEffort

from core.config import get_settings
from core.exceptions import DegradedCapabilityError
from core.serde import parse_json
from services.endpoint_spec import EndpointSpec
from services.llm import descriptor_of, positive_int
from services.providers.base import ModelLimits, Provider, ProviderPreset
from services.reasoning import ModelDescriptor

_DEFAULT_BASE_URL = "https://api.anthropic.com"
_FIRST_PARTY_HOST = "api.anthropic.com"
# Pinned rather than left to the model, whose default varies by model (Opus 5.5: medium).
_DEFAULT_EFFORT: ThinkingEffort = "high"
# The Messages API demands a pinned version header on every request.
_API_VERSION = "2023-06-01"
_TIMEOUT = httpx.Timeout(8.0, connect=3.0)


def _headers(api_key: str | None) -> dict[str, str]:
    headers = {"anthropic-version": _API_VERSION}
    if api_key:
        headers["x-api-key"] = api_key
    return headers


def _models_url(base_url: str) -> str:
    root = base_url.rstrip("/")
    # The operator may paste the root or the versioned URL; normalize to /v1/models.
    if not root.endswith("/v1"):
        root += "/v1"
    return root + "/models"


class AnthropicNativeProvider(Provider):
    id: str = "anthropic"
    display_name: str = "Anthropic"
    requires_key: bool = True
    preset: ProviderPreset = ProviderPreset(
        default_base_url=_DEFAULT_BASE_URL,
        key_hint="sk-ant-…",
        docs_url="https://console.anthropic.com/settings/keys",
        vision=True,
    )

    @override
    def build_model(self, spec: EndpointSpec) -> Model:
        provider = _SdkProvider(api_key=spec.api_key, base_url=spec.base_url)
        settings: AnthropicModelSettings = self.model_settings(descriptor_of(spec))
        if spec.max_output_tokens is not None:
            # The model's own ceiling in place of the library's 4096 (`Provider
            # .model_limits`); a request's own `max_tokens` still wins.
            settings = {**settings, "max_tokens": spec.max_output_tokens}
        return AnthropicModel(
            spec.model,
            provider=provider,
            settings=settings,
            # Our window when we have one, the library's otherwise (`Provider.build_model`).
            profile=(
                ModelProfile(context_window=spec.context_window)
                if spec.context_window is not None
                else None
            ),
        )

    @override
    async def discover(
        self, base_url: str, api_key: str | None, *, client: httpx.AsyncClient | None = None
    ) -> list[str]:
        try:
            payload = await self._list_models(base_url, api_key, client=client)
        except (httpx.HTTPError, ValueError) as exc:
            raise DegradedCapabilityError(
                f"could not list models from {base_url!r}: {exc}"
            ) from exc
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            raise DegradedCapabilityError(f"{base_url!r} returned an unrecognized models payload")
        ids: list[str] = []
        for row in data:
            if isinstance(row, dict):
                model_id = row.get("id")
                if isinstance(model_id, str) and model_id:
                    ids.append(model_id)
        return sorted(dict.fromkeys(ids))

    @override
    async def probe(
        self, base_url: str, api_key: str | None, *, client: httpx.AsyncClient | None = None
    ) -> None:
        _ = await self._list_models(base_url, api_key, client=client)

    @override
    async def context_window(
        self,
        base_url: str,
        api_key: str | None,
        model: str,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> int | None:
        """The model's ``max_input_tokens``, read off its models-API entry."""
        return (await self.model_limits(base_url, api_key, model, client=client)).context_window

    @override
    async def model_limits(
        self,
        base_url: str,
        api_key: str | None,
        model: str,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> ModelLimits:
        """Both limits from the model's own ``GET /v1/models/{id}`` entry:
        ``max_input_tokens`` is the context window, ``max_tokens`` the largest output
        one request may ask for.

        Read from the API rather than a hard-coded table of known Anthropic models. Such
        a table would be right until the day it silently isn't — a new model, or a beta
        that extends an existing one — and a limit that is confidently wrong is worse
        than one that admits it doesn't know: the operator would only find out by hitting
        a ceiling the meter said was far away. An Anthropic-compatible proxy that serves
        no such entry, or one without these fields, answers None for whatever is missing,
        and the library's own figures stand."""
        try:
            row = await self._get(
                f"{_models_url(base_url)}/{quote(model, safe='')}", api_key, client=client
            )
        except httpx.HTTPError, ValueError:
            return ModelLimits()
        if not isinstance(row, dict):
            return ModelLimits()
        return ModelLimits(
            context_window=positive_int(row.get("max_input_tokens")),
            max_output_tokens=positive_int(row.get("max_tokens")),
        )

    async def _list_models(
        self, base_url: str, api_key: str | None, *, client: httpx.AsyncClient | None = None
    ) -> JsonValue:
        return await self._get(_models_url(base_url), api_key, client=client)

    async def _get(
        self, url: str, api_key: str | None, *, client: httpx.AsyncClient | None = None
    ) -> JsonValue:
        http = client or httpx.AsyncClient(follow_redirects=True)
        try:
            response = await http.get(url, headers=_headers(api_key), timeout=_TIMEOUT)
            _ = response.raise_for_status()
            return parse_json(response.content)
        finally:
            if client is None:
                await http.aclose()

    @override
    def reasoning_off(self, descriptor: ModelDescriptor) -> ModelSettings:
        # Unified `False` thinks as little as the model allows where it can't turn
        # thinking off (Claude Opus 5.5), so background calls never run at chat effort.
        return {"thinking": False}

    @override
    def model_settings(self, descriptor: ModelDescriptor) -> AnthropicModelSettings:
        """Prompt-cache breakpoints, pinned effort and, on the first-party API, cache
        diagnostics.

        The breakpoints are the automatic one that walks forward with the conversation,
        plus the tool array and the static instructions. ``anthropic_cache_messages`` is
        absent on purpose: it replaces the automatic breakpoint for gateways that reject
        it. :meth:`run_settings` raises the TTL for attended runs.

        Effort goes through the unified ``thinking`` setting, not ``anthropic_effort``:
        Pydantic AI then sends a mid-conversation change as a per-message effort entry
        where the model supports one, which keeps the cached history valid.
        """
        settings: AnthropicModelSettings = {
            **_cache_settings(_ttl(attended=False)),
            "thinking": descriptor.effort or _DEFAULT_EFFORT,
        }
        if _is_first_party(descriptor.base_url):
            settings["anthropic_cache_diagnostics"] = True
        return settings

    @override
    def run_settings(self, *, attended: bool) -> AnthropicModelSettings:
        return _cache_settings(_ttl(attended=attended))


def _ttl(*, attended: bool) -> Literal["5m", "1h"] | None:
    """The configured TTL resolved for one run; None when caching is switched off."""
    ttl = get_settings().anthropic_cache_ttl
    if ttl == "off":
        return None
    if ttl == "auto":
        return "1h" if attended else "5m"
    return ttl


def _cache_settings(ttl: Literal["5m", "1h"] | None) -> AnthropicModelSettings:
    # "off" sends no keys at all: a proxy that rejects `cache_control` fails the whole
    # request, so a disabled-looking value is not enough.
    if ttl is None:
        return {}
    return {
        "anthropic_cache": ttl,
        "anthropic_cache_instructions": ttl,
        "anthropic_cache_tool_definitions": ttl,
    }


def _is_first_party(base_url: str | None) -> bool:
    # Diagnostics is a Claude API field; a proxy in front of it may reject it.
    return urlparse(base_url or _DEFAULT_BASE_URL).hostname == _FIRST_PARTY_HOST


PROVIDER = AnthropicNativeProvider()
