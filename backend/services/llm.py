"""Model construction — turn an endpoint spec (or a chain) into a Pydantic AI model.

Roles the engine consumes: ``main`` (chat/agent), ``utility`` (cheap background
work), ``embedding`` (recall). Resolution of a *role* to a model is the
**registry's** job (:mod:`services.registry`, the single source of truth — manual
config today, an automatic-setup write path later). This module owns
the layer below it: dispatching one spec to its **provider adapter**
(:mod:`services.providers` — OpenAI-compatible, Anthropic, Google, local) and
wrapping an ordered chain in ``FallbackModel``. Both registry-sourced and
Every endpoint flows through these builders. The OpenAI-wire discovery
and probe helpers live here too — they are what the openai-compatible and local
adapters delegate to.

**The AE-5.3 rule — "don't switch endpoints once answer text has streamed" — is
ours, not the library's.** ``FallbackModel`` only ever falls back while *opening*
a request stream (a dead/erroring endpoint before any output); once a stream is
open and answer text is flowing it propagates errors rather than re-trying a
different endpoint. We complete the guarantee in the orchestrator: a model error
after the first ``answer.delta`` ends the run (``run.answer_started`` is set in
the translation layer) — we never re-drive a turn onto another endpoint once the
user has seen partial output.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

import httpx
from pydantic_ai.models import Model
from pydantic_ai.models.fallback import FallbackModel
from pydantic_ai.settings import ModelSettings

from core.exceptions import DegradedCapabilityError
from runs import Endpoint, EndpointGate
from services import reasoning
from services.gated_model import EndpointGatedModel

logger = logging.getLogger(__name__)

ROLES = frozenset({"main", "utility", "embedding"})
# Roles that drive the agent loop must support native tool-calling (AE-8.1).
TOOL_CALLING_ROLES = frozenset({"main", "utility"})

# Parallel tool calls, on for every endpoint: independent calls go out in one assistant
# message and the library runs the batch concurrently, so a three-search step costs one
# model round-trip instead of three. A flat default rather than a per-provider shape
# (unlike reasoning-off) because both model classes that read the field omit it from a
# request carrying no tools — so a toolless agent is untouched and no server is handed an
# argument for a request with nothing to parallelize.
TOOL_CALL_SETTINGS: ModelSettings = {"parallel_tool_calls": True}


def with_parallel_tools(settings: ModelSettings | None = None) -> ModelSettings:
    """``settings`` with parallel tool calling folded in, for call sites that already
    pass their own. An explicit caller value wins, so a deliberate opt-out survives."""
    return {**TOOL_CALL_SETTINGS, **(settings or {})}


@dataclass(frozen=True)
class EndpointSpec:
    """A resolved, decrypted endpoint — everything needed to build a model."""

    base_url: str
    model: str
    # Which adapter builds the model (`services/providers`). Every pre-provider
    # endpoint is OpenAI-compatible, hence the default.
    provider: str = "openai-compatible"
    # None ⇒ the server doesn't authenticate (a local engine). Never a sentinel.
    api_key: str | None = None
    context_window: int | None = None
    # The most output tokens one response may ask for, as the provider advertises it —
    # discovered, never operator-set. None ⇒ unknown, and the model library's own
    # default stands (`Provider.model_limits` has why that matters for Anthropic).
    max_output_tokens: int | None = None
    native_tools: bool = True
    vision: bool = False
    thinking: bool = False
    # The stored endpoint this spec was resolved from; None for a spec built by hand.
    endpoint_id: str | None = None
    name: str = ""
    # The operator's cap on concurrent runs, else the slot count the server reports.
    max_concurrent_runs: int | None = None
    parallel_slots: int | None = None

    def gate_endpoint(self) -> Endpoint | None:
        """This endpoint as the run gate sees it, or None when it is not a stored one."""
        if self.endpoint_id is None:
            return None
        return Endpoint(
            id=self.endpoint_id,
            name=self.name,
            capacity=self.max_concurrent_runs or self.parallel_slots,
        )


def descriptor_of(spec: EndpointSpec) -> reasoning.ModelDescriptor:
    """The facts a provider matches a model *family* on, read off a resolved spec.

    One home for the projection so the registry's reasoning-off lookup and an adapter's
    own settings hook are demonstrably asking about the same model: two hand-built
    descriptors would be two places for a field to be forgotten, and the symptom would be
    a lever silently not pulled rather than an error.
    """
    return reasoning.ModelDescriptor(
        model_id=spec.model, base_url=spec.base_url, thinking=spec.thinking
    )


def build_model(spec: EndpointSpec) -> Model:
    """Build one Pydantic AI model from an endpoint spec, via its provider adapter."""
    # Deferred import: the adapters type against EndpointSpec, so the registry loads
    # on first build rather than at module import.
    from services.providers import get_provider

    return get_provider(spec.provider).build_model(spec)


def build_chain(specs: Sequence[EndpointSpec], gate: EndpointGate | None = None) -> Model:
    """Build a model for an ordered fallback chain.

    One endpoint resolves to a plain model; two or more are wrapped in
    ``FallbackModel`` (tried in order on connection/HTTP failure — pre-answer
    only, per AE-5.3). An empty chain is a degraded capability. With a ``gate``, the
    primary's requests wait in its endpoint's line (`services/gated_model.py`).
    """
    if not specs:
        raise DegradedCapabilityError("no endpoints in the model chain")
    models = [build_model(spec) for spec in specs]
    primary = specs[0].gate_endpoint()
    if gate is not None and primary is not None:
        models[0] = EndpointGatedModel(models[0], gate, primary)
    if len(models) == 1:
        return models[0]
    return FallbackModel(*models)


async def discover_openai_models(
    base_url: str,
    api_key: str | None = None,
    *,
    client: httpx.AsyncClient | None = None,
) -> list[str]:
    """Discover the model ids an OpenAI-wire server advertises.

    Hits the OpenAI-style ``GET {base_url}/models`` — the de-facto standard most
    OpenAI-compatible servers (Ollama, vLLM, LM Studio, …) expose — and dispatches
    the body through per-shape adapters, so servers that instead return a
    ``models`` array (or a bare list) still resolve. Reuses the caller's pooled
    ``client`` when given (one per app, connection-reused), else a transient one.

    Returns the ids de-duplicated and sorted — possibly **empty** when the
    server has a models API that lists nothing. Raises ``DegradedCapabilityError``
    only when the server can't be reached or returns an unrecognized payload, so
    the caller distinguishes "supported but empty" from "no models API".
    """
    url = base_url.rstrip("/") + "/models"
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    # Short connect timeout so an unreachable host fails fast; the read budget is
    # larger for slow-but-alive providers.
    timeout = httpx.Timeout(8.0, connect=3.0)
    http = client or httpx.AsyncClient(follow_redirects=True)
    try:
        response = await http.get(url, headers=headers, timeout=timeout)
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise DegradedCapabilityError(f"could not list models from {base_url!r}: {exc}") from exc
    finally:
        if client is None:
            await http.aclose()
    ids = _extract_model_ids(payload)
    if ids is None:
        raise DegradedCapabilityError(f"{base_url!r} returned an unrecognized models payload")
    return ids


async def probe_openai_endpoint(
    base_url: str,
    api_key: str | None = None,
    *,
    client: httpx.AsyncClient | None = None,
) -> None:
    """Check an OpenAI-wire endpoint's reachability + auth with one lightweight request.

    Unlike :func:`discover_openai_models` — which collapses every failure into
    ``DegradedCapabilityError`` — this lets the **typed** httpx error propagate so the
    caller (the registry's connection test) can tell auth from rate-limit from timeout
    from unreachable. Probes the same ``GET {base_url}/models`` discovery uses. Returns
    ``None`` on a healthy, parseable 2xx; otherwise raises ``httpx.HTTPStatusError``
    (non-2xx), ``httpx.TimeoutException``, ``httpx.ConnectError`` / other
    ``httpx.TransportError``, or ``ValueError`` (a body that isn't JSON).
    """
    url = base_url.rstrip("/") + "/models"
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    timeout = httpx.Timeout(8.0, connect=3.0)
    http = client or httpx.AsyncClient(follow_redirects=True)
    try:
        response = await http.get(url, headers=headers, timeout=timeout)
        response.raise_for_status()
        response.json()  # a recognized provider answers JSON; a non-JSON body is bad_response
    finally:
        if client is None:
            await http.aclose()


# The keys OpenAI-wire servers use for a model's *maximum* context window, in the order
# we trust them. There is no standard — the OpenAI `/v1/models` schema has no such field
# at all — so each server that bothers to report one invented its own name. vLLM says
# `max_model_len`, LM Studio `max_context_length`, llama.cpp-derived servers `n_ctx`,
# and several gateways `context_length`. Reading all of them is what makes discovery
# work across "OpenAI-compatible" servers that agree on nothing but the chat route.
_MAX_CONTEXT_KEYS = (
    "context_length",
    "max_context_length",
    "max_model_len",
    "context_window",
    "n_ctx",
)
# The keys that report the window a model is *currently loaded with*, which is a
# different fact and the one that matters: a model loaded at 32k on a server that could
# do 256k has a real ceiling of 32k, and every guard built on the window has to measure
# the limit the next turn will hit rather than the one the hardware would allow.
_LOADED_CONTEXT_KEYS = ("loaded_context_length",)
# LM Studio reports nothing useful on the OpenAI route but exposes its own richer
# listing alongside it. Checked only after the standard route comes back silent.
_LMSTUDIO_NATIVE = "/api/v0/models"
# llama.cpp's own status route. It is not a model listing at all — the server runs one
# model and reports its live generation settings — which is why the listing probes above
# cannot reach it: llama.cpp's `/v1/models` row is keyed by a file path or alias that
# rarely equals the model name the operator bound, and carries no context field anyway.
_LLAMA_CPP_PROPS = "/props"


def positive_int(value: object) -> int | None:
    """``value`` if it is a usable token count, else None.

    Guards `bool` explicitly: it is an `int` subclass, and a server answering
    `"context_length": true` would otherwise yield a one-token window — a gauge pinned
    at 100% forever and a thread that can never send."""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _context_from_row(row: object, keys: Sequence[str]) -> int | None:
    """A positive context length from a model listing entry, whichever of ``keys`` it
    used. Split by *kind* of key rather than read in one pass, because a loaded window
    found on the second probe still has to beat a maximum found on the first."""
    if not isinstance(row, dict):
        return None
    for key in keys:
        window = positive_int(row.get(key))
        if window is not None:
            return window
    return None


def _find_model_row(payload: object, model: str) -> object | None:
    """The listing entry describing ``model``, across the same shapes
    :func:`_extract_model_ids` understands."""
    rows: list[object] = []
    if isinstance(payload, dict):
        for key in ("data", "models"):
            if isinstance(payload.get(key), list):
                rows = payload[key]
                break
    elif isinstance(payload, list):
        rows = payload
    for row in rows:
        if not isinstance(row, dict):
            continue
        ident = row.get("id") or row.get("name")
        if isinstance(ident, str) and ident.removeprefix("models/") == model:
            return row
    return None


async def discover_openai_context_window(
    base_url: str,
    model: str,
    api_key: str | None = None,
    *,
    client: httpx.AsyncClient | None = None,
) -> int | None:
    """The context window an OpenAI-wire server reports for ``model``, or None."""
    window, _ = await discover_openai_limits(base_url, model, api_key, client=client)
    return window


async def discover_openai_limits(
    base_url: str,
    model: str,
    api_key: str | None = None,
    *,
    client: httpx.AsyncClient | None = None,
) -> tuple[int | None, int | None]:
    """The context window an OpenAI-wire server reports for ``model``, and the number of
    parallel slots it runs; None for whichever it does not say.

    **Never raises.** A window we can't discover is not an error — it is a fact the
    server didn't state, and every caller's answer to that is the same (fall back to
    whatever the operator configured). Collapsing the failure here keeps that decision
    in one place instead of at each call site.

    Three probes, because "OpenAI-compatible" servers agree on the chat route and nothing
    else. The standard listing first; then LM Studio's native listing, which reports a
    window where the OpenAI route reports none; then llama.cpp's `/props`. The last two
    are the reason this is worth doing at all for local servers: the OpenAI `/v1/models`
    schema has no context field, so the most common local setups can never answer on that
    route.

    **A loaded window beats a maximum**, whichever probe found it. The two are different
    facts — what this server *could* run and what it *is* running — and only the second
    is the ceiling the next turn hits. That is why a maximum found early doesn't return
    immediately: it is held as the fallback while the remaining probes are given their
    chance to report the real one.
    """
    maximum: int | None = None
    native_row: object | None = None
    loaded: int | None = None
    for url, is_native in (
        (base_url.rstrip("/") + "/models", False),
        (_native_listing_url(base_url), True),
    ):
        if url is None:
            continue
        row = _find_model_row(await _get_json(url, api_key, client=client), model)
        if is_native:
            native_row = row
        loaded = _context_from_row(row, _LOADED_CONTEXT_KEYS)
        if loaded is not None:
            break
        if maximum is None:
            maximum = _context_from_row(row, _MAX_CONTEXT_KEYS)
    # The cache posture is worth nothing except in a log beside a slow turn, so that is where
    # it goes. Debug, like the rest of the prefix diagnostic: an installation property nobody
    # is investigating is noise. Read from whichever source answered — a llama.cpp server's
    # own `/props`, or the native listing row already fetched above.
    posture = await _llama_cpp_props(base_url, api_key, client=client) or _native_posture(
        native_row
    )
    if posture is not None:
        logger.debug("prompt cache posture base=%s %s", base_url, posture.summary())
    slots = posture.total_slots if posture is not None else None
    if loaded is not None:
        return loaded, slots
    if posture is not None and posture.context_window is not None:
        return posture.context_window, slots
    return maximum, slots


#: Settings a llama.cpp-derived server may report that decide what happens to a slot's KV
#: cache when the slot is retasked — which is the whole of whether a background utility call
#: on the same server costs the chat thread its cached prefix. `cache_ram` (and its `-1` for
#: unlimited) checkpoints a displaced slot into host memory instead of destroying it;
#: `cache_reuse` is what lets an *insertion* near the front of the prompt — a revealed tool
#: group, a permission-level line — be recovered by shifting the KV rather than re-read;
#: `kv_unified` decides whether `-c` is divided between slots or shared.
#:
#: Read by name rather than schema because there is no schema: `/props` is llama.cpp's own
#: route, its shape moves between builds, and forks report their own subsets. So what is
#: recorded is *whichever of these the server mentioned*, at whatever value — a fact about
#: what the server said, never an inference about what it does.
_PROMPT_CACHE_KEYS = (
    "cache_ram",
    "cache_ram_mib",
    "cache_reuse",
    "cache_idle_slots",
    "kv_unified",
)


@dataclass(frozen=True, slots=True)
class PromptCachePosture:
    """What a local server said about how it treats a prompt prefix between requests.

    Diagnostic only — nothing branches on it. It exists because the *magnitude* of one of
    the three causes of a slow local turn (a background call on the shared endpoint evicting
    the chat's cached prefix) depends entirely on the build the operator is running, and that
    is the one input to the diagnosis this codebase cannot reason its way to. An older
    single-slot llama.cpp with no host-RAM checkpoint loses the chat's KV on every review; a
    current build with `--cache-ram` and a unified KV largely does not.

    Every field is optional because every field is a thing a given server may simply not
    say, and absent has to read as "did not say" rather than as zero.

    **What to change on the server, if these read badly.** Written here rather than in an
    operator guide because it is advice about exactly these flags, and a guide kept anywhere
    else goes stale without anything noticing:

    - ``--cache-ram -1`` (or a value at least the size of the KV cache) plus a unified KV, so
      a slot retasked by a background call is *checkpointed* into host memory rather than
      destroyed. This is the whole of what makes sharing one server with the ``utility`` role
      survivable. Note a dense 32B at f16 with 60k tokens of state is around 15 GB, well past
      the 8 GB a build may default to.
    - ``--cache-reuse 256``, which is what recovers an **insertion** near the front of the
      prompt by shifting the KV instead of re-reading everything behind it. That is the shape
      a revealed tool group and a mid-turn permission-level change both have.
    - And the one with no flag: **editing the project's own brief mid-thread costs a full
      re-prefill.** An agent that rewrites ``CLAUDE.md`` in its own worktree has changed the
      head of every subsequent request in that thread.

    Deliberately **not** the older advice to raise ``-np``. Under ``-np auto`` with a unified
    KV, ``-c`` is not divided between slots and that guidance is simply wrong for current
    builds. vLLM's block cache is shared across requests, which makes it the better local
    engine for Auto-level work.

    **The flags above are a ``llama-server`` command line, and most local setups do not have
    one.** LM Studio runs llama.cpp and MLX as embedded runtimes with no such switches (and no
    ``/props``), so on one of those the only lever an operator has is to bind ``utility`` to a
    second endpoint — which is what ``ModelRegistry.background_shares_main`` exists to point
    at. It is still worth reporting which runtime is serving, because it is what says whose
    documentation applies.

    **An earlier version of this said MLX has no prompt cache and that a second process was
    the only fix there. Measured against LM Studio 2.37, that is wrong**: on a 5,936-token
    prompt an identical replay took 0.37s against 13.63s cold, and a request whose *last*
    paragraph differed took 3.20s against 9.09s for one whose *first* sentence did — two
    equally novel requests of the same length, which is what rules out a response cache and
    leaves prefix reuse as the only explanation. Both runtimes also retained more than one
    prefix. So the head/tail rule pays on either, and the per-engine advice here is about
    which knobs exist, not about whether caching happens.
    """

    context_window: int | None = None
    #: How many parallel slots the server runs. One slot means every background call is
    #: served by evicting whatever the chat thread left there.
    total_slots: int | None = None
    #: The build string, verbatim. The only handle on "which llama.cpp is this".
    build_info: str | None = None
    #: The inference engine behind this model where the server names it (`gguf` ⇒ llama.cpp,
    #: `mlx` ⇒ Apple's MLX) — and the field most often available, since it comes from a model
    #: listing rather than from a server command line.
    #:
    #: It says **whose knobs apply**, not whether caching happens: both were measured reusing
    #: a matching prefix and retaining more than one of them, so the head/tail rule pays on
    #: either. What differs is that the `/props` flags above exist only for a real
    #: `llama-server` — an embedded runtime has none, and there the only lever is binding
    #: `utility` to a second endpoint.
    backend: str | None = None
    #: Whichever of :data:`_PROMPT_CACHE_KEYS` the server reported, and what it said.
    cache_settings: Mapping[str, object] = field(default_factory=dict)

    def summary(self) -> str:
        """One line for the diagnostic log."""
        stated = " ".join(f"{key}={value}" for key, value in sorted(self.cache_settings.items()))
        return (
            f"n_ctx={self.context_window} backend={self.backend or '?'} "
            f"slots={self.total_slots} build={self.build_info or '?'} "
            f"{stated or 'no cache settings reported'}"
        )


#: Where a model listing row may name its inference engine. LM Studio says
#: `compatibility_type`; its completions responses say `model_info.format`. Both carry the
#: same two values this cares about, `gguf` and `mlx`.
_BACKEND_KEYS = ("compatibility_type", "format")


def _native_posture(row: object) -> PromptCachePosture | None:
    """A cache posture from a model listing row, for the servers that have no ``/props``.

    Written because the probe below was **inert on the most common local setup**. LM Studio
    answers `/props` with an "unexpected endpoint" error, and its OpenAI-shaped completions
    carry no `timings` block — yet its own listing states the two facts that matter, and the
    window probe is already fetching it. Reading them there costs no extra request.

    What it cannot state is the slot count or the cache flags: those are a llama.cpp
    *server* command line, and LM Studio does not run one. Absent, as always, reads as "did
    not say" — the backend alone is enough to tell an operator whether there is a prompt
    cache to lose.
    """
    if not isinstance(row, dict):
        return None
    backend = next(
        (str(row[key]) for key in _BACKEND_KEYS if isinstance(row.get(key), str)), None
    )
    window = _context_from_row(row, (*_LOADED_CONTEXT_KEYS, *_MAX_CONTEXT_KEYS))
    if backend is None and window is None:
        return None
    return PromptCachePosture(context_window=window, backend=backend)


async def _llama_cpp_props(
    base_url: str, api_key: str | None, *, client: httpx.AsyncClient | None = None
) -> PromptCachePosture | None:
    """llama.cpp's own `/props`, read for the context window **and** its cache posture.

    `default_generation_settings.n_ctx` is the per-slot window a request actually gets
    (llama.cpp divides the total context between its parallel slots unless the KV is
    unified), so it is a *loaded* figure in the strong sense — the number the very next
    completion will be measured against.

    The three cache-posture facts ride along because the request is already being made. They
    are read from the top level and from the generation settings alike, since which of the
    two carries a given knob has moved between builds and neither placement is promised.

    Tried at the root as well as under the base URL: llama.cpp registers `/props` at the
    top level while the endpoint an operator configures ends in `/v1`, so the obvious URL
    is the wrong one on the most common setup. Both are cheap best-effort GETs that
    answer None on anything unexpected."""
    for url in _props_urls(base_url):
        payload = await _get_json(url, api_key, client=client)
        if not isinstance(payload, dict):
            continue
        settings = payload.get("default_generation_settings")
        settings = settings if isinstance(settings, dict) else {}
        window = positive_int(settings.get("n_ctx"))
        if window is None:
            continue
        build = payload.get("build_info")
        stated = {
            key: source[key]
            for source in (payload, settings)
            for key in _PROMPT_CACHE_KEYS
            if key in source
        }
        return PromptCachePosture(
            context_window=window,
            total_slots=positive_int(payload.get("total_slots")),
            build_info=build if isinstance(build, str) else None,
            cache_settings=stated,
        )
    return None


def _props_urls(base_url: str) -> tuple[str, ...]:
    """Where `/props` might live for this base URL, most likely first — de-duplicated,
    so a base that carries no version segment costs one request rather than two."""
    trimmed = base_url.rstrip("/")
    urls = [trimmed + _LLAMA_CPP_PROPS]
    if trimmed.endswith("/v1"):
        urls.insert(0, trimmed[: -len("/v1")] + _LLAMA_CPP_PROPS)
    return tuple(dict.fromkeys(urls))


def _native_listing_url(base_url: str) -> str | None:
    """LM Studio's own listing, derived from an OpenAI base URL by swapping the
    version segment. Only attempted for a `/v1` base — anything else is a server
    whose native API (if it has one) we know nothing about."""
    trimmed = base_url.rstrip("/")
    if not trimmed.endswith("/v1"):
        return None
    return trimmed[: -len("/v1")] + _LMSTUDIO_NATIVE


async def _get_json(
    url: str, api_key: str | None, *, client: httpx.AsyncClient | None = None
) -> object | None:
    """A best-effort JSON GET for discovery's optional extras — None on any failure.
    Short timeouts: this rides on paths the operator is waiting on, and a window we
    didn't get is survivable where a stall is not."""
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    http = client or httpx.AsyncClient(follow_redirects=True)
    try:
        response = await http.get(url, headers=headers, timeout=httpx.Timeout(5.0, connect=2.0))
        response.raise_for_status()
        return response.json()
    except httpx.HTTPError, ValueError:
        return None
    finally:
        if client is None:
            await http.aclose()


def _extract_model_ids(payload: object) -> list[str] | None:
    """Pull model identifiers out of whichever shape a provider returned.

    Each adapter recognizes one convention and returns its ids, or ``None`` if the
    payload isn't its shape; the first match wins. ``None`` overall means no shape
    matched (an unrecognized payload); an empty list means a recognized response
    that simply lists no models. Splitting the adapters keeps provider-specific
    quirks (Gemini's ``models/`` name prefix) from mangling other providers' ids.
    """
    for adapter in (_openai_models, _named_models, _bare_list):
        ids = adapter(payload)
        if ids is not None:
            return sorted(dict.fromkeys(ids))
    return None


def _openai_models(payload: object) -> list[str] | None:
    """OpenAI/Anthropic and most OpenAI-compatible servers: ``{"data": [{"id"}]}``."""
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        return None
    return [
        row["id"]
        for row in payload["data"]
        if isinstance(row, dict) and isinstance(row.get("id"), str) and row["id"]
    ]


def _named_models(payload: object) -> list[str] | None:
    """Gemini/Cohere/Ollama-native: ``{"models": [{"id" | "name"}]}``. Strips the
    ``models/`` prefix Gemini puts on names — scoped here so it can't touch an
    OpenAI-shaped id that legitimately starts with ``models/``."""
    if not isinstance(payload, dict) or not isinstance(payload.get("models"), list):
        return None
    ids: list[str] = []
    for row in payload["models"]:
        if not isinstance(row, dict):
            continue
        ident = row.get("id") or row.get("name")
        if isinstance(ident, str):
            stripped = ident.removeprefix("models/")
            if stripped:
                ids.append(stripped)
    return ids


def _bare_list(payload: object) -> list[str] | None:
    """Some servers return a bare list — of id strings, or of ``{"id" | "name"}``."""
    if not isinstance(payload, list):
        return None
    ids: list[str] = []
    for row in payload:
        if isinstance(row, str):
            ident: str | None = row
        elif isinstance(row, dict):
            ident = row.get("id") or row.get("name")
        else:
            ident = None
        if isinstance(ident, str) and ident:
            ids.append(ident)
    return ids
