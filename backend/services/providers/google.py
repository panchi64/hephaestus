"""Google — native adapter over Pydantic AI's ``GoogleModel`` (Gemini API).

Talks the Gemini generateContent protocol directly through ``google-genai`` instead
of the OpenAI-compat shim, so native tool use and thinking budgets work as Google
defines them.
"""

from __future__ import annotations

from typing import override

import httpx
from pydantic import JsonValue
from pydantic_ai.models import Model
from pydantic_ai.models.google import GoogleModel, GoogleModelSettings
from pydantic_ai.profiles import ModelProfile
from pydantic_ai.providers.google import GoogleProvider as _SdkProvider

from core.exceptions import DegradedCapabilityError
from core.serde import parse_json
from services.endpoint_spec import EndpointSpec
from services.providers.base import ModelLimits, Provider, ProviderPreset
from services.reasoning import ModelDescriptor

_DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com"
_TIMEOUT = httpx.Timeout(8.0, connect=3.0)


def _models_url(base_url: str) -> str:
    root = base_url.rstrip("/")
    if not root.endswith("/v1beta"):
        root += "/v1beta"
    return root + "/models"


class GoogleNativeProvider(Provider):
    id: str = "google"
    display_name: str = "Google"
    requires_key: bool = True
    preset: ProviderPreset = ProviderPreset(
        default_base_url=_DEFAULT_BASE_URL,
        key_hint="AIza…",
        docs_url="https://aistudio.google.com/apikey",
        vision=True,
    )

    @override
    def build_model(self, spec: EndpointSpec) -> Model:
        base_url = spec.base_url if spec.base_url.rstrip("/") != _DEFAULT_BASE_URL else None
        provider = _SdkProvider(api_key=spec.api_key or "", base_url=base_url)
        # Our window when we have one, the library's otherwise — `Provider.build_model`
        # has the reasoning. This adapter talks to one lab, so a model name really does
        # name that model. Partial either way: every other inferred capability stands.
        return GoogleModel(
            spec.model,
            provider=provider,
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
        models = payload.get("models") if isinstance(payload, dict) else None
        if not isinstance(models, list):
            raise DegradedCapabilityError(f"{base_url!r} returned an unrecognized models payload")
        # Gemini names come as "models/<id>" — the stored/served id is the bare one.
        ids: list[str] = []
        for row in models:
            if isinstance(row, dict):
                name = row.get("name")
                if isinstance(name, str) and name:
                    ids.append(name.removeprefix("models/"))
        return sorted(dict.fromkeys(i for i in ids if i))

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
        """Gemini states this outright — ``inputTokenLimit`` on each listing entry —
        so no key-guessing is needed here, unlike the OpenAI-wire adapter.

        The *input* limit and not input+output: the gauge measures what the next turn
        must fit into its prompt, which is exactly the input ceiling."""
        try:
            payload = await self._list_models(base_url, api_key, client=client)
        except httpx.HTTPError, ValueError:
            return None
        models = payload.get("models") if isinstance(payload, dict) else None
        if not isinstance(models, list):
            return None
        for row in models:
            if not isinstance(row, dict):
                continue
            name = row.get("name")
            if not isinstance(name, str) or name.removeprefix("models/") != model:
                continue
            limit = row.get("inputTokenLimit")
            if isinstance(limit, int) and not isinstance(limit, bool) and limit > 0:
                return limit
        return None

    @override
    async def model_limits(
        self,
        base_url: str,
        api_key: str | None,
        model: str,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> ModelLimits:
        # The window only: `GoogleModel` leaves an absent output cap off the request, so
        # the server's own limit already applies and there is nothing to replace.
        return ModelLimits(
            context_window=await self.context_window(base_url, api_key, model, client=client)
        )

    async def _list_models(
        self, base_url: str, api_key: str | None, *, client: httpx.AsyncClient | None = None
    ) -> JsonValue:
        headers = {"x-goog-api-key": api_key} if api_key else {}
        http = client or httpx.AsyncClient(follow_redirects=True)
        try:
            response = await http.get(_models_url(base_url), headers=headers, timeout=_TIMEOUT)
            _ = response.raise_for_status()
            return parse_json(response.content)
        finally:
            if client is None:
                await http.aclose()

    @override
    def reasoning_off(self, descriptor: ModelDescriptor) -> GoogleModelSettings:
        # Only the Flash/Lite family accepts a zero thinking budget; Pro enforces a
        # floor and rejects 0, so an unrecognized Gemini is left to think.
        model_id = descriptor.model_id.lower()
        if "flash" in model_id or "lite" in model_id:
            return {"google_thinking_config": {"thinking_budget": 0}}
        return {}


PROVIDER = GoogleNativeProvider()
