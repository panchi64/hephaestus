"""First-run walkthrough state, and the local model servers it offers before asking for a URL."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass

import httpx

from core.exceptions import DegradedCapabilityError
from services import llm
from services.settings_store import SettingsStore

ONBOARDING_COMPLETED_KEY = "onboarding.completed"


@dataclass(frozen=True, slots=True)
class LocalCandidate:
    label: str
    base_url: str


# Ollama is deliberately absent: it is not a supported engine, even behind an endpoint.
LOCAL_CANDIDATES: tuple[LocalCandidate, ...] = (
    LocalCandidate("LM Studio", "http://127.0.0.1:1234/v1"),
    LocalCandidate("llama-server", "http://127.0.0.1:8080/v1"),
)

_PROBE_TIMEOUT_S = 3.0


@dataclass(frozen=True, slots=True)
class LocalModel:
    id: str
    context_window: int | None
    parallel_slots: int | None
    #: The server reports this model as loaded right now; None when it doesn't say.
    loaded: bool | None
    #: `llm`, `vlm` or `embeddings` where the server names it.
    kind: str | None


@dataclass(frozen=True, slots=True)
class LocalServer:
    label: str
    base_url: str
    models: list[LocalModel]


async def mark_pending(store: SettingsStore, owner_id: str) -> None:
    await store.set(owner_id, ONBOARDING_COMPLETED_KEY, "false")


async def mark_completed(store: SettingsStore, owner_id: str) -> None:
    await store.set(owner_id, ONBOARDING_COMPLETED_KEY, "true")


async def is_completed(store: SettingsStore, owner_id: str) -> bool:
    """A workspace set up before the walkthrough existed has no key, and counts as done."""
    return await store.get(owner_id, ONBOARDING_COMPLETED_KEY) != "false"


def _local_model(
    model_id: str, limits: tuple[int | None, int | None], row: Mapping[str, object] | None
) -> LocalModel:
    state = row.get("state") if row else None
    kind = row.get("type") if row else None
    return LocalModel(
        id=model_id,
        context_window=limits[0],
        parallel_slots=limits[1],
        loaded=state == "loaded" if isinstance(state, str) else None,
        kind=kind if isinstance(kind, str) else None,
    )


async def _probe(candidate: LocalCandidate, client: httpx.AsyncClient | None) -> LocalServer | None:
    try:
        async with asyncio.timeout(_PROBE_TIMEOUT_S):
            await llm.probe_openai_endpoint(candidate.base_url, client=client)
            ids = await llm.discover_openai_models(candidate.base_url, client=client)
    except TimeoutError, httpx.HTTPError, ValueError, DegradedCapabilityError:
        return None
    rows = await llm.lmstudio_listing(candidate.base_url, client=client)
    limits = await asyncio.gather(
        *(
            llm.discover_openai_limits(candidate.base_url, model_id, client=client)
            for model_id in ids
        )
    )
    models = [
        _local_model(model_id, limit, rows.get(model_id))
        for model_id, limit in zip(ids, limits, strict=True)
    ]
    return LocalServer(candidate.label, candidate.base_url, models)


async def detect_local(
    client: httpx.AsyncClient | None = None,
    candidates: tuple[LocalCandidate, ...] = LOCAL_CANDIDATES,
) -> list[LocalServer]:
    """The candidates that answered, in candidate order. An unreachable one is left out."""
    found = await asyncio.gather(*(_probe(candidate, client) for candidate in candidates))
    return [server for server in found if server is not None]
