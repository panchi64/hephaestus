"""Model construction: turn an endpoint spec (or a chain) into a Pydantic AI model.

Roles the engine consumes: ``main`` (chat/agent), ``utility`` (cheap background work),
``embedding`` (recall). Resolving a role to a model is the registry's job
(:mod:`services.registry`); this module dispatches one spec to its provider adapter
(:mod:`services.providers`) and wraps an ordered chain in ``FallbackModel``. The
OpenAI-wire discovery and probe helpers live here because the openai-compatible and local
adapters delegate to them.

**Fallback happens only while opening a stream.** ``FallbackModel`` falls back on a
dead or erroring endpoint before any output; once answer text is flowing it propagates the
error. The orchestrator (``agent/turn.py``) then retracts the partial answer
(``answer.retracted``) and re-sends, which opens a fresh stream that may land on the next
endpoint.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

import httpx
from pydantic_ai.models import Model
from pydantic_ai.models.fallback import FallbackModel
from pydantic_ai.settings import ModelSettings

from core.exceptions import DegradedCapabilityError
from core.serde import parse_json
from runs import EndpointGate
from services import reasoning
from services.endpoint_spec import EndpointSpec
from services.gated_model import EndpointGatedModel
from services.providers import get_provider
from services.shapes import as_dict, as_list, dict_list

logger = logging.getLogger(__name__)

ROLES = frozenset({"main", "utility", "embedding", "decision"})
# Roles that drive the agent loop must support native tool-calling.
TOOL_CALLING_ROLES = frozenset({"main", "utility"})

# Parallel tool calls, on for every endpoint so independent calls cost one round-trip. A
# flat default, not per-provider, because both model classes that read the field omit it
# from a request with no tools.
TOOL_CALL_SETTINGS: ModelSettings = {"parallel_tool_calls": True}


def with_parallel_tools(settings: ModelSettings | None = None) -> ModelSettings:
    """``settings`` with parallel tool calling folded in, for call sites that already
    pass their own. An explicit caller value wins, so a deliberate opt-out survives."""
    return {**TOOL_CALL_SETTINGS, **(settings or {})}


def descriptor_of(spec: EndpointSpec) -> reasoning.ModelDescriptor:
    """The facts a provider matches a model family on, read off a resolved spec.

    Shared by the registry's reasoning-off lookup and an adapter's settings hook so a
    forgotten field cannot silently skip the lever in one of them.
    """
    return reasoning.ModelDescriptor(
        model_id=spec.model, base_url=spec.base_url, thinking=spec.thinking
    )


def build_model(spec: EndpointSpec) -> Model:
    """Build one Pydantic AI model from an endpoint spec, via its provider adapter."""
    return get_provider(spec.provider).build_model(spec)


def build_chain(specs: Sequence[EndpointSpec], gate: EndpointGate | None = None) -> Model:
    """Build a model for an ordered fallback chain.

    One endpoint resolves to a plain model; two or more are wrapped in
    ``FallbackModel`` (tried in order on connection/HTTP failure, pre-answer only). An
    empty chain is a degraded capability. With a ``gate``, the primary's requests wait in
    its endpoint's line (`services/gated_model.py`).
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

    Hits ``GET {base_url}/models`` and dispatches the body through per-shape adapters, so
    servers returning a ``models`` array or a bare list still resolve. Reuses the caller's
    pooled ``client`` when given, else a transient one.

    Returns the ids de-duplicated and sorted, possibly empty when the server has a models
    API that lists nothing. Raises ``DegradedCapabilityError`` only when the server can't
    be reached or returns an unrecognized payload, so the caller can tell "supported but
    empty" from "no models API".
    """
    url = base_url.rstrip("/") + "/models"
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    # Short connect timeout so an unreachable host fails fast.
    timeout = httpx.Timeout(8.0, connect=3.0)
    http = client or httpx.AsyncClient(follow_redirects=True)
    try:
        response = await http.get(url, headers=headers, timeout=timeout)
        _ = response.raise_for_status()
        payload = parse_json(response.content)
    except (httpx.HTTPError, ValueError) as exc:
        raise DegradedCapabilityError(f"could not list models from {base_url!r}: {exc}") from exc
    finally:
        if client is None:
            await http.aclose()
    ids = extract_model_ids(payload)
    if ids is None:
        raise DegradedCapabilityError(f"{base_url!r} returned an unrecognized models payload")
    return ids


async def probe_openai_endpoint(
    base_url: str,
    api_key: str | None = None,
    *,
    client: httpx.AsyncClient | None = None,
) -> None:
    """Check an OpenAI-wire endpoint's reachability and auth with one ``GET {base_url}/models``.

    Unlike :func:`discover_openai_models`, the typed httpx error propagates so the
    registry's connection test can tell auth from rate-limit, timeout and unreachable.
    Returns ``None`` on a parseable 2xx; otherwise raises ``httpx.HTTPStatusError``,
    ``httpx.TimeoutException``, ``httpx.ConnectError`` / other ``httpx.TransportError``, or
    ``ValueError`` (a non-JSON body).
    """
    url = base_url.rstrip("/") + "/models"
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    timeout = httpx.Timeout(8.0, connect=3.0)
    http = client or httpx.AsyncClient(follow_redirects=True)
    try:
        response = await http.get(url, headers=headers, timeout=timeout)
        _ = response.raise_for_status()
        _ = parse_json(response.content)
    finally:
        if client is None:
            await http.aclose()


# Keys OpenAI-wire servers use for a model's maximum context window, in trust order. The
# `/v1/models` schema has no such field, so each server invented a name: vLLM
# `max_model_len`, LM Studio `max_context_length`, llama.cpp-derived `n_ctx`, gateways
# `context_length`.
_MAX_CONTEXT_KEYS = (
    "context_length",
    "max_context_length",
    "max_model_len",
    "context_window",
    "n_ctx",
)
# Keys reporting the window a model is currently loaded with. This is the real ceiling
# (a model loaded at 32k on a 256k-capable server), which every window guard must measure.
_LOADED_CONTEXT_KEYS = ("loaded_context_length",)
# LM Studio's own listing, checked only after the standard route comes back silent.
_LMSTUDIO_NATIVE = "/api/v0/models"
# llama.cpp's status route, a live-settings report rather than a listing: its
# `/v1/models` row is keyed by a file path or alias and has no context field.
_LLAMA_CPP_PROPS = "/props"


def positive_int(value: object) -> int | None:
    """``value`` if it is a usable token count, else None.

    Rejects `bool` explicitly: as an `int` subclass, `"context_length": true` would yield a
    one-token window."""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _context_from_row(row: object, keys: Sequence[str]) -> int | None:
    """A positive context length from a listing entry, whichever of ``keys`` it used.
    Callers pass loaded and maximum keys separately because a loaded window found on a
    later probe must still beat an earlier maximum."""
    entry = as_dict(row)
    if entry is None:
        return None
    for key in keys:
        window = positive_int(entry.get(key))
        if window is not None:
            return window
    return None


def _find_model_row(payload: object, model: str) -> object | None:
    """The listing entry describing ``model``, across the same shapes
    :func:`extract_model_ids` understands."""
    rows: list[object] = []
    mapping = as_dict(payload)
    if mapping is not None:
        for key in ("data", "models"):
            listed = as_list(mapping.get(key))
            if listed is not None:
                rows = listed
                break
    else:
        rows = as_list(payload) or []
    for row in rows:
        entry = as_dict(row)
        if entry is None:
            continue
        ident = entry.get("id") or entry.get("name")
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

    **Never raises.** An undiscoverable window is a fact the server didn't state, and every
    caller falls back to the operator's configured value.

    Three probes, since "OpenAI-compatible" servers agree on nothing but the chat route: the
    standard listing, then LM Studio's native listing, then llama.cpp's `/props`. The last
    two matter because the OpenAI `/v1/models` schema has no context field.

    **A loaded window beats a maximum**, whichever probe found it: only what the server is
    running is the ceiling the next turn hits. A maximum found early is held as the
    fallback while the remaining probes look for the loaded one.
    """
    standard_row = _find_model_row(
        await _get_json(base_url.rstrip("/") + "/models", api_key, client=client), model
    )
    native_row: object | None = None
    native_url = _native_listing_url(base_url)
    if native_url is not None and _context_from_row(standard_row, _LOADED_CONTEXT_KEYS) is None:
        native_row = _find_model_row(await _get_json(native_url, api_key, client=client), model)
    # Diagnostic only, logged at debug: from llama.cpp's `/props` or the native row above.
    posture = await llama_cpp_props(base_url, api_key, client=client) or native_posture(native_row)
    if posture is not None:
        logger.debug("prompt cache posture base=%s %s", base_url, posture.summary())
    return _resolve_limits(standard_row, native_row, posture)


async def discover_openai_limits_many(
    base_url: str,
    models: Sequence[str],
    api_key: str | None = None,
    *,
    client: httpx.AsyncClient | None = None,
) -> dict[str, tuple[int | None, int | None]]:
    """:func:`discover_openai_limits` for every model a server lists, reading each listing
    once rather than once per model. Never raises."""
    native_url = _native_listing_url(base_url)
    standard, native, props = await asyncio.gather(
        _get_json(base_url.rstrip("/") + "/models", api_key, client=client),
        _get_json(native_url, api_key, client=client) if native_url else _absent(),
        llama_cpp_props(base_url, api_key, client=client),
    )
    limits: dict[str, tuple[int | None, int | None]] = {}
    for model in models:
        native_row = _find_model_row(native, model)
        limits[model] = _resolve_limits(
            _find_model_row(standard, model), native_row, props or native_posture(native_row)
        )
    return limits


async def _absent() -> None:
    return None


def _resolve_limits(
    standard_row: object | None, native_row: object | None, posture: PromptCachePosture | None
) -> tuple[int | None, int | None]:
    """A loaded window from either listing, else the posture's window, else a maximum."""
    slots = posture.total_slots if posture is not None else None
    maximum: int | None = None
    for row in (standard_row, native_row):
        loaded = _context_from_row(row, _LOADED_CONTEXT_KEYS)
        if loaded is not None:
            return loaded, slots
        if maximum is None:
            maximum = _context_from_row(row, _MAX_CONTEXT_KEYS)
    if posture is not None and posture.context_window is not None:
        return posture.context_window, slots
    return maximum, slots


#: Settings a llama.cpp-derived server may report that decide what happens to a slot's KV
#: cache when retasked, i.e. whether a background utility call costs the chat thread its
#: cached prefix. `cache_ram` (`-1` for unlimited) checkpoints a displaced slot into host
#: memory; `cache_reuse` recovers an insertion near the front of the prompt (a revealed
#: tool group, a permission-level line) by shifting the KV; `kv_unified` decides whether
#: `-c` is divided between slots.
#:
#: Read by name because `/props` has no schema (it moves between builds and forks report
#: subsets); what is recorded is whichever of these the server mentioned.
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

    Diagnostic only; nothing branches on it. Whether a background call on the shared
    endpoint evicts the chat's cached prefix depends on the build: an older single-slot
    llama.cpp with no host-RAM checkpoint loses the chat's KV on every review, a current
    build with `--cache-ram` and a unified KV largely does not. Every field is optional,
    and absent means "did not say", not zero.

    If these read badly, change on the server:

    - ``--cache-ram -1`` (or at least the KV cache's size) plus a unified KV, so a slot
      retasked by a background call is checkpointed into host memory rather than destroyed;
      this is what makes sharing one server with the ``utility`` role survivable. A dense
      32B at f16 with 60k tokens of state is around 15 GB, past the 8 GB default.
    - ``--cache-reuse 256``, which recovers an insertion near the front of the prompt (a
      revealed tool group, a mid-turn permission-level change) by shifting the KV.
    - Flagless: editing the project's own brief mid-thread costs a full re-prefill, since an
      agent rewriting ``CLAUDE.md`` in its worktree changes the head of every later request.

    Raising ``-np`` is not advice: under ``-np auto`` with a unified KV, ``-c`` is not
    divided between slots. vLLM's block cache is shared across requests, making it the
    better local engine for Auto-level work.

    The flags are a ``llama-server`` command line. LM Studio runs llama.cpp and MLX as
    embedded runtimes with no such switches (and no ``/props``), so there the only lever is
    binding ``utility`` to a second endpoint (``ModelRegistry.background_shares_main``).
    Both runtimes were measured reusing a matching prefix on LM Studio 2.37 (a 5,936-token
    replay took 0.37s against 13.63s cold; a differing last paragraph 3.20s against 9.09s
    for a differing first sentence), so the head/tail rule pays on either.
    """

    context_window: int | None = None
    #: How many parallel slots the server runs. One slot means every background call is
    #: served by evicting whatever the chat thread left there.
    total_slots: int | None = None
    #: The build string, verbatim.
    build_info: str | None = None
    #: The inference engine where the server names it (`gguf` is llama.cpp, `mlx` is
    #: Apple's MLX); the most often available field, since a model listing carries it.
    #: It says whose knobs apply: the `/props` flags above exist only for a real
    #: `llama-server`.
    backend: str | None = None
    #: Whichever of :data:`_PROMPT_CACHE_KEYS` the server reported, and what it said.
    cache_settings: Mapping[str, object] = field(default_factory=dict[str, object])

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


def native_posture(row: object) -> PromptCachePosture | None:
    """A cache posture from a model listing row, for the servers that have no ``/props``.

    LM Studio answers `/props` with an error, but its own listing states the backend and
    window, and the window probe already fetches it. It cannot state the slot count or cache
    flags, which belong to a llama.cpp server command line; the backend alone tells an
    operator whether there is a prompt cache to lose.
    """
    entry = as_dict(row)
    if entry is None:
        return None
    backend = next(
        (str(entry[key]) for key in _BACKEND_KEYS if isinstance(entry.get(key), str)), None
    )
    window = _context_from_row(entry, (*_LOADED_CONTEXT_KEYS, *_MAX_CONTEXT_KEYS))
    if backend is None and window is None:
        return None
    return PromptCachePosture(context_window=window, backend=backend)


async def llama_cpp_props(
    base_url: str, api_key: str | None, *, client: httpx.AsyncClient | None = None
) -> PromptCachePosture | None:
    """llama.cpp's own `/props`, read for the context window and its cache posture.

    `default_generation_settings.n_ctx` is the per-slot window a request actually gets
    (the total is divided between slots unless the KV is unified), so it is a loaded
    figure. Cache knobs are read from the top level and the generation settings alike,
    since builds differ on which carries them.

    Tried at the root as well as under the base URL, since llama.cpp registers `/props` at
    the top level while a configured endpoint ends in `/v1`. Best-effort GETs answering None
    on anything unexpected."""
    for url in _props_urls(base_url):
        payload = as_dict(await _get_json(url, api_key, client=client))
        if payload is None:
            continue
        settings = as_dict(payload.get("default_generation_settings")) or {}
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
    """Where `/props` might live for this base URL, most likely first, de-duplicated."""
    trimmed = base_url.rstrip("/")
    urls = [trimmed + _LLAMA_CPP_PROPS]
    if trimmed.endswith("/v1"):
        urls.insert(0, trimmed[: -len("/v1")] + _LLAMA_CPP_PROPS)
    return tuple(dict.fromkeys(urls))


def _native_listing_url(base_url: str) -> str | None:
    """LM Studio's own listing, derived by swapping the `/v1` segment; None for any other base."""
    trimmed = base_url.rstrip("/")
    if not trimmed.endswith("/v1"):
        return None
    return trimmed[: -len("/v1")] + _LMSTUDIO_NATIVE


async def lmstudio_listing(
    base_url: str, api_key: str | None = None, *, client: httpx.AsyncClient | None = None
) -> dict[str, Mapping[str, object]]:
    """LM Studio's native model rows keyed by id, carrying `type` and `state`; empty for
    any other server or on failure."""
    url = _native_listing_url(base_url)
    if url is None:
        return {}
    payload = as_dict(await _get_json(url, api_key, client=client))
    rows = dict_list(payload.get("data")) if payload else []
    return {model_id: row for row in rows if isinstance(model_id := row.get("id"), str)}


async def _get_json(
    url: str, api_key: str | None, *, client: httpx.AsyncClient | None = None
) -> object | None:
    """A best-effort JSON GET for discovery's optional extras, None on any failure. Short
    timeouts, since the operator is waiting on these paths."""
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    http = client or httpx.AsyncClient(follow_redirects=True)
    try:
        response = await http.get(url, headers=headers, timeout=httpx.Timeout(5.0, connect=2.0))
        _ = response.raise_for_status()
        return parse_json(response.content)
    except httpx.HTTPError, ValueError:
        return None
    finally:
        if client is None:
            await http.aclose()


def extract_model_ids(payload: object) -> list[str] | None:
    """Pull model identifiers out of whichever shape a provider returned.

    Each adapter recognizes one convention and returns its ids, or ``None`` if the payload
    isn't its shape; the first match wins. ``None`` overall means no shape matched; an empty
    list means a recognized response listing no models. Separate adapters keep quirks like
    Gemini's ``models/`` prefix from mangling other providers' ids.
    """
    for adapter in (_openai_models, _named_models, _bare_list):
        ids = adapter(payload)
        if ids is not None:
            return sorted(dict.fromkeys(ids))
    return None


def _openai_models(payload: object) -> list[str] | None:
    """OpenAI/Anthropic and most OpenAI-compatible servers: ``{"data": [{"id"}]}``."""
    mapping = as_dict(payload)
    rows = as_list(mapping.get("data")) if mapping is not None else None
    if rows is None:
        return None
    ids: list[str] = []
    for row in rows:
        entry = as_dict(row)
        ident = entry.get("id") if entry is not None else None
        if isinstance(ident, str) and ident:
            ids.append(ident)
    return ids


def _named_models(payload: object) -> list[str] | None:
    """Gemini/Cohere/Ollama-native: ``{"models": [{"id" | "name"}]}``. Strips the
    ``models/`` prefix Gemini puts on names, scoped here so an OpenAI-shaped id that
    legitimately starts with it is untouched."""
    mapping = as_dict(payload)
    rows = as_list(mapping.get("models")) if mapping is not None else None
    if rows is None:
        return None
    ids: list[str] = []
    for row in rows:
        entry = as_dict(row)
        if entry is None:
            continue
        ident = entry.get("id") or entry.get("name")
        if isinstance(ident, str):
            stripped = ident.removeprefix("models/")
            if stripped:
                ids.append(stripped)
    return ids


def _bare_list(payload: object) -> list[str] | None:
    """Some servers return a bare list — of id strings, or of ``{"id" | "name"}``."""
    rows = as_list(payload)
    if rows is None:
        return None
    ids: list[str] = []
    for row in rows:
        entry = as_dict(row)
        ident: object
        if isinstance(row, str):
            ident = row
        elif entry is not None:
            ident = entry.get("id") or entry.get("name")
        else:
            ident = None
        if isinstance(ident, str) and ident:
            ids.append(ident)
    return ids
