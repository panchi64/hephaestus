"""The provider contract — what one model-API family knows how to do.

A *provider* is the adapter between an operator's endpoint row and a concrete
Pydantic AI model class: it builds the model, lists/probes the remote catalog,
declares whether a key is required, ships the preset the frontend prefills a new
endpoint from, and knows how to turn reasoning off for its own models. Everything
above (the registry's role resolution, fallback chains, encryption) is
provider-agnostic; everything provider-specific lives in one adapter module.

Adapters register by presence: a module in this package exposing ``PROVIDER``
is discovered by the pkgutil walk in ``__init__`` — the same contract as the
feature manifests. Adding a lab is one file.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    import httpx
    from pydantic_ai.models import Model
    from pydantic_ai.settings import ModelSettings

    from services.endpoint_spec import EndpointSpec
    from services.reasoning import ModelDescriptor


@dataclass(frozen=True)
class ProviderPreset:
    """What the frontend prefills when the operator picks this provider — served by
    ``GET /models/providers`` so the client never hardcodes a lab's details."""

    # Prefilled base URL; None ⇒ the operator must supply one (a self-hosted server).
    default_base_url: str | None = None
    # A hint for the key field (e.g. "sk-ant-…"); None ⇒ no key expected.
    key_hint: str | None = None
    # Where the operator gets a key / reads the docs.
    docs_url: str | None = None
    # Capability defaults for a fresh endpoint of this kind.
    native_tools: bool = True
    vision: bool = False


@dataclass(frozen=True)
class ModelLimits:
    """What a provider reports about one model's size limits — None wherever it doesn't
    say. Discovered, never tabled: a hard-coded figure is right until the day a new
    model or a beta quietly makes it wrong."""

    # The input ceiling a prompt must fit — what the gauge and the fold trigger measure.
    context_window: int | None = None
    # The largest `max_tokens` one response may ask for.
    max_output_tokens: int | None = None
    # How many requests the server runs in parallel (a llama.cpp server's slots).
    parallel_slots: int | None = None


@runtime_checkable
class Provider(Protocol):
    """One model-API family. Stateless; safe to share across requests."""

    # Stable identifier persisted on ModelEndpoint.provider.
    id: str
    display_name: str
    # Whether an endpoint of this kind must carry an API key — validated at save.
    requires_key: bool
    preset: ProviderPreset

    def build_model(self, spec: EndpointSpec) -> Model:
        """A concrete Pydantic AI model from a resolved, decrypted spec.

        **Every adapter puts ``spec.context_window`` on the model's profile**, and this
        is the one place the reason is written down. Pydantic AI resolves a profile's
        ``context_window`` from its own price data when no layer states one — which
        answers a question about a *model name*, not about a *server*, and those differ
        exactly where this product lives. A llama.cpp instance started with ``-c 8192``
        advertises its architecture's 128k ceiling, and any endpoint named after a
        well-known model inherits that model's number no matter what is behind the base
        URL. The window that matters is the one the server will accept.

        ``spec.context_window`` is the answer to the right question: the registry funnels
        every resolution through ``_with_model_limits``, which prefers the operator's
        own figure and otherwise probes the server. **When it is set, it wins** — in
        every adapter.

        Where the adapters differ is what to do when it is ``None``, and the split is
        not arbitrary: it is whether this adapter's base URL identifies the vendor.
        ``anthropic`` and ``google`` talk to one lab's API, so the model name really does
        name that model and the library's figure is the best available — it is left
        alone. ``openai-compatible`` fronts *any* server, so a recognised name is a
        coincidence of what the operator typed and the guess is actively wrong; there the
        profile is pinned to ``None``. Unknown is the honest state and the one the rest of
        the codebase already handles: an endpoint that declares no window never folds,
        because folding on a guess compacts a thread that was never under pressure.
        """
        ...

    async def discover(
        self, base_url: str, api_key: str | None, *, client: httpx.AsyncClient | None = None
    ) -> list[str]:
        """The model ids the remote advertises — possibly empty. Raises
        ``DegradedCapabilityError`` when unreachable or unrecognized (the caller
        distinguishes "supported but empty" from "no models API"). ``client`` is an
        optional pooled ``httpx.AsyncClient`` to reuse; None ⇒ a transient one."""
        ...

    async def probe(
        self, base_url: str, api_key: str | None, *, client: httpx.AsyncClient | None = None
    ) -> None:
        """One lightweight reachability+auth check. Unlike ``discover`` this lets the
        **typed** httpx error propagate so the registry's connection test can tell
        auth from rate-limit from timeout from unreachable."""
        ...

    async def context_window(
        self,
        base_url: str,
        api_key: str | None,
        model: str,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> int | None:
        """The context window ``model`` reports, or None when this provider has no way
        to say — which is the common case, not the exception.

        **Never raises.** A window is a fact the provider either states or doesn't; a
        failure to reach it is indistinguishable, to every caller, from it not being
        there, and both answers are "fall back to what the operator configured".

        Per *model*, not per endpoint, because that is the grain of the truth: one
        OpenAI-compatible server happily serves a 256k model and a 32k model at the
        same base URL, and a window cached against the endpoint would be wrong for one
        of them."""
        ...

    async def model_limits(
        self,
        base_url: str,
        api_key: str | None,
        model: str,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> ModelLimits:
        """Every limit ``model`` reports, in one lookup — what the registry asks.

        The window from :meth:`context_window`, plus the largest ``max_tokens`` one
        response may request. One call rather than one per fact because a provider that
        states both states them on the same catalog row, and asking twice would be two
        round-trips for one answer. An adapter that knows only the window returns
        ``ModelLimits(context_window=await self.context_window(...))``.

        The output ceiling exists for one library behavior. Pydantic AI's
        ``AnthropicModel`` must send ``max_tokens`` — the Messages API requires it — and
        substitutes 4096 when no layer supplied one, so every answer and every summary
        a caller left unbounded is silently cut at 4096 tokens. The model's own
        advertised maximum is the honest reading of "unbounded"; ``build_model`` puts it
        on the model's standing settings, where a request's own ``max_tokens`` still
        wins. The OpenAI and Gemini wires treat an absent value as the server's own
        limit, so there is nothing to supply and they report None.

        **Never raises**, for the reason :meth:`context_window` gives."""
        ...

    def reasoning_off(self, descriptor: ModelDescriptor) -> ModelSettings:
        """The settings that stop this provider's model from spending tokens on
        thinking for background work — ``{}`` when there is nothing to turn off."""
        ...

    def model_settings(self, descriptor: ModelDescriptor) -> ModelSettings:
        """Standing settings every request to this provider's models carries.

        Distinct from :meth:`reasoning_off`, which one *caller* asks for when it wants a
        cheap background pass. These are the model's own defaults — a lab-wide request
        shape that is true whoever is calling — so the adapter hands them to the model
        **at construction** (``Model(settings=…)``) rather than to any one call site.
        That placement is the whole point: a setting applied there survives a fallback
        chain, a parked turn resumed hours later, and every path that builds its own
        per-request settings, none of which have to know the lab exists. Pydantic AI
        merges a request's settings *over* the model's, so a caller can still override
        one deliberately.

        Defaulted rather than required: most families need nothing here, and an adapter
        that says nothing means ``{}``.
        """
        return {}
