"""Anthropic — native adapter over Pydantic AI's ``AnthropicModel``.

Speaks the Messages API directly (system prompts, tool use, and thinking blocks in
their native shape) instead of squeezing Claude through an OpenAI-compat gateway.
``base_url`` is honored, so an Anthropic-compatible proxy still works.
"""

from __future__ import annotations

from urllib.parse import quote

import httpx
from pydantic_ai.models import Model
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.profiles import ModelProfile
from pydantic_ai.providers.anthropic import AnthropicProvider as _SdkProvider
from pydantic_ai.settings import ModelSettings

from core.config import get_settings
from core.exceptions import DegradedCapabilityError
from services.llm import EndpointSpec, descriptor_of, positive_int
from services.providers.base import ModelLimits, ProviderPreset
from services.reasoning import ModelDescriptor

_DEFAULT_BASE_URL = "https://api.anthropic.com"
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


class AnthropicNativeProvider:
    id = "anthropic"
    display_name = "Anthropic"
    requires_key = True
    preset = ProviderPreset(
        default_base_url=_DEFAULT_BASE_URL,
        key_hint="sk-ant-…",
        docs_url="https://console.anthropic.com/settings/keys",
        vision=True,
    )

    def build_model(self, spec: EndpointSpec) -> Model:
        provider = _SdkProvider(api_key=spec.api_key, base_url=spec.base_url)
        settings = self.model_settings(descriptor_of(spec))
        if spec.max_output_tokens is not None:
            # The model's own ceiling in place of the library's 4096 — `Provider
            # .model_limits` has why. On the construction-time settings for the reason
            # `model_settings` gives, which is also what lets a caller that does want a
            # bound still set one per request.
            settings = {**settings, "max_tokens": spec.max_output_tokens}
        return AnthropicModel(
            spec.model,
            provider=provider,
            settings=settings,
            # Our window when we have one, the library's otherwise — `Provider
            # .build_model` has the reasoning. This adapter talks to one lab, so a model
            # name really does name that model and the library's figure is the best
            # available when the models API couldn't be asked, or a proxy in front of it
            # doesn't say. Partial either way: every other inferred capability of the
            # named model stands.
            profile=(
                ModelProfile(context_window=spec.context_window)
                if spec.context_window is not None
                else None
            ),
        )

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
        ids = [
            row["id"]
            for row in data
            if isinstance(row, dict) and isinstance(row.get("id"), str) and row["id"]
        ]
        return sorted(dict.fromkeys(ids))

    async def probe(
        self, base_url: str, api_key: str | None, *, client: httpx.AsyncClient | None = None
    ) -> None:
        await self._list_models(base_url, api_key, client=client)

    async def context_window(
        self, base_url: str, api_key: str | None, model: str, *, client=None
    ) -> int | None:
        """The model's ``max_input_tokens``, read off its models-API entry."""
        return (await self.model_limits(base_url, api_key, model, client=client)).context_window

    async def model_limits(
        self, base_url: str, api_key: str | None, model: str, *, client=None
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
    ) -> object:
        return await self._get(_models_url(base_url), api_key, client=client)

    async def _get(
        self, url: str, api_key: str | None, *, client: httpx.AsyncClient | None = None
    ) -> object:
        http = client or httpx.AsyncClient(follow_redirects=True)
        try:
            response = await http.get(url, headers=_headers(api_key), timeout=_TIMEOUT)
            response.raise_for_status()
            return response.json()
        finally:
            if client is None:
                await http.aclose()

    def reasoning_off(self, descriptor: ModelDescriptor) -> ModelSettings:
        # Claude's extended thinking is opt-in per request — a background call that
        # asks for nothing gets nothing, so there is no lever to pull here.
        return {}

    def model_settings(self, descriptor: ModelDescriptor) -> ModelSettings:
        """Prompt-cache breakpoints on the three prefixes a chat turn repeats verbatim.

        A conversation re-sends its whole history on every turn, and in front of that
        history sit two blocks that barely change at all: the standing instructions and
        the tool catalog — about fourteen thousand tokens of schema before the first word
        of the thread. Without a breakpoint every turn pays full price to re-read them.

        The three settings are the three prefixes, and they compose: ``anthropic_cache``
        is the automatic breakpoint that walks forward as the conversation grows,
        ``anthropic_cache_instructions`` pins the system block and
        ``anthropic_cache_tool_definitions`` pins the tool array. Only
        ``anthropic_cache_messages`` conflicts with the automatic one, and it is
        deliberately absent — it is the fallback for gateways that reject the top-level
        parameter, not a fourth breakpoint.

        Nothing here depends on ``descriptor``: caching is a property of the Messages
        API, not of one Claude model, so every model this adapter builds gets it.

        ``anthropic_cache_ttl = "off"`` returns **no settings at all** rather than a
        disabled-looking trio. ``base_url`` is honored here, so this adapter also fronts
        Anthropic-compatible proxies, and one that rejects ``cache_control`` fails the
        request outright — an operator behind such a proxy needs a way to stop sending the
        breakpoints, not a cheaper tier of them.
        """
        ttl = get_settings().anthropic_cache_ttl
        if ttl == "off":
            return {}
        return {
            "anthropic_cache": ttl,
            "anthropic_cache_instructions": ttl,
            "anthropic_cache_tool_definitions": ttl,
        }


PROVIDER = AnthropicNativeProvider()
