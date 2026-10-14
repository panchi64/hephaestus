"""The embedding capability — turn text into vectors for semantic recall.

A thin async interface over the configured ``embedding``-role endpoint (resolved
from the model registry). It calls the provider's OpenAI-compatible
``/embeddings`` API directly, since embeddings are not a Pydantic AI chat model.

Pluggable by design (the :class:`Embedder` protocol): the real implementation
talks to a model server; tests inject a deterministic fake. **Graceful
degradation (`XC-DEG-1`):** when no embedding endpoint is configured the embedder
is *unavailable* and raises :class:`~core.exceptions.DegradedCapabilityError`,
which the memory store catches to fall back to keyword recall (`MEM-2`).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import numpy as np
from openai import APIConnectionError, AsyncOpenAI
from sqlalchemy import Engine
from sqlmodel import Session

from core.db import in_session
from core.exceptions import DegradedCapabilityError
from core.vault import Vault
from services import llm

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EmbeddingBatch:
    """Vectors plus the provenance every stored vector records (`EMB-2`)."""

    vectors: list[list[float]]
    model: str
    dim: int


@runtime_checkable
class Embedder(Protocol):
    async def is_available(self, owner_id: str) -> bool: ...

    async def embed(self, owner_id: str, texts: list[str]) -> EmbeddingBatch: ...


class EmbeddingSpecSource(Protocol):
    """What the embedder needs from the model registry."""

    async def resolve_embedding_spec(self, owner_id: str) -> llm.EndpointSpec: ...


class RegistryEmbedder:
    """Embeds via the operator's configured ``embedding``-role endpoint."""

    def __init__(self, registry: EmbeddingSpecSource) -> None:
        self._registry = registry

    async def is_available(self, owner_id: str) -> bool:
        try:
            await self._registry.resolve_embedding_spec(owner_id)
            return True
        except DegradedCapabilityError:
            return False

    async def embed(self, owner_id: str, texts: list[str]) -> EmbeddingBatch:
        # Raises DegradedCapabilityError when unconfigured — the caller degrades.
        spec = await self._registry.resolve_embedding_spec(owner_id)
        # The OpenAI client refuses a None key; a keyless local server gets a
        # placeholder header it ignores (same adapter-boundary quirk as the chat path).
        #
        # `async with`, like `probe_embedding` below and every other transient client in
        # the services layer, because a client that isn't closed strands its httpx
        # connection pool and sockets — on the *failure* path most of all, which is the
        # one that repeats. It stays per-call rather than becoming one pooled client even
        # though this is hot (once per stored item and once per recall): the spec is
        # re-resolved every call and legitimately changes under us when the operator
        # rebinds the embedding role or a managed server returns on a new port, so a
        # shared client would need keying, invalidation, and a close at shutdown that
        # nothing here owns. What that buys is one connection setup on a call otherwise
        # dominated by model inference.
        async with AsyncOpenAI(base_url=spec.base_url, api_key=spec.api_key or "unused") as client:
            try:
                response = await client.embeddings.create(model=spec.model, input=texts)
            except APIConnectionError as exc:
                # Nothing is listening. That is the capability being *degraded*, not a
                # fault to report as one — a local model server the operator hasn't
                # started is the ordinary case, and every caller already handles degraded
                # by falling back to keyword recall. Raised as such it stays silent;
                # raised raw it buries a 60-line traceback in the log for a routine
                # condition, and never names the address it failed to reach, which is the
                # one fact needed to fix it. The validation path (`probe_embedding`)
                # already draws this line — runtime has to draw it the same way or the
                # same failure has two different characters. One line, not silence: "not
                # configured" is genuinely unremarkable, but "configured and unreachable"
                # is a thing the operator can fix, and semantic recall staying quietly off
                # is how it goes unnoticed for weeks.
                logger.warning(
                    "embedding endpoint %s is unreachable — semantic recall is off until "
                    "it is back; keyword recall still covers these messages",
                    spec.base_url,
                )
                raise DegradedCapabilityError(
                    f"couldn't reach embedding endpoint {spec.base_url!r}"
                ) from exc
        vectors = [item.embedding for item in response.data]
        dim = len(vectors[0]) if vectors else 0
        return EmbeddingBatch(vectors=vectors, model=spec.model, dim=dim)


async def embed_query(
    embedder: Embedder, owner_id: str, query: str
) -> tuple[np.ndarray | None, str | None]:
    """Embed a single query for hybrid recall, returning ``(vector, model)``.

    A degraded embedder (no endpoint, or a query that can't embed) yields
    ``(None, None)`` so the caller collapses to keyword-only — the `EMB-2`
    fallback shared by every recall path."""
    try:
        batch = await embedder.embed(owner_id, [query])
    except DegradedCapabilityError:
        return None, None
    except Exception:
        # A transient embedder failure (timeout, 5xx) is "semantic search
        # unavailable" too — degrade to keyword rather than failing the recall.
        logger.exception("query embedding failed; falling back to keyword recall")
        return None, None
    return np.asarray(batch.vectors[0], dtype=np.float64), batch.model


def encode_vector(vault: Vault, vector: list[float]) -> str:
    """Seal a float vector for storage (the inverse of :func:`decode_vector`)."""
    return vault.encrypt_str(json.dumps(vector))


def decode_vector(vault: Vault, embedding_enc: str) -> list[float]:
    """Open a sealed embedding back into its float vector."""
    return json.loads(vault.decrypt_str(embedding_enc))


async def embed_and_seal_rows(
    *,
    engine: Engine,
    vault: Vault,
    embedder: Embedder,
    owner_id: str,
    model_cls: type,
    pending: list[tuple[str, str]],
    batch_size: int = 64,
) -> int:
    """Embed ``(row_id, text)`` pairs in batches and seal each vector back onto its
    row's ``embedding_enc``/``embedding_model``/``embedding_dim`` columns. ``model_cls``
    is the SQLModel table carrying those columns.

    The one (re-)indexing loop shared by memory and conversations, so the
    embed→seal→write step lives once. Best-effort: a degraded or failing embedder
    stops the run and leaves the rest for next time. Returns how many rows were sealed."""
    embedded = 0
    for start in range(0, len(pending), batch_size):
        chunk = pending[start : start + batch_size]
        try:
            batch = await embedder.embed(owner_id, [text for _id, text in chunk])
        except DegradedCapabilityError:
            break  # embedder went away mid-run — leave the rest for next time
        except Exception:
            logger.exception("embedding rows failed; leaving the rest for next time")
            break
        # Seal up front so the DB closure only carries plain row updates.
        updates = [
            (rid, encode_vector(vault, vector), batch.model, batch.dim)
            for (rid, _text), vector in zip(chunk, batch.vectors, strict=False)
        ]

        def write(session: Session, updates: list[tuple[str, str, str, int]] = updates) -> None:
            for rid, vector_enc, model, dim in updates:
                row = session.get(model_cls, rid)
                if row is not None:
                    row.embedding_enc = vector_enc
                    row.embedding_model = model
                    row.embedding_dim = dim

        await in_session(engine, write)
        embedded += len(chunk)
    return embedded


async def probe_embedding(spec: llm.EndpointSpec) -> int:
    """One real ``/embeddings`` call to confirm an endpoint+model actually serves
    vectors — the bind-time check the ``embedding`` role lacked, so a chat model (or
    an unreachable server) can't be silently bound and degrade recall to keyword-only.

    Returns the vector dimension. Raises :class:`DegradedCapabilityError` when the
    endpoint can't be reached, or when it answers but returns no usable vector (not an
    embeddings model). The two cases carry distinct messages so the operator can tell
    a wrong model from a down server."""
    async with AsyncOpenAI(base_url=spec.base_url, api_key=spec.api_key or "unused") as client:
        try:
            response = await client.embeddings.create(model=spec.model, input=["probe"])
        except APIConnectionError as exc:
            raise DegradedCapabilityError(
                f"couldn't reach endpoint {spec.base_url!r} to validate the embedding model"
            ) from exc
        except Exception as exc:
            raise DegradedCapabilityError(
                f"model {spec.model!r} did not accept an embeddings request "
                "(is it an embeddings model?)"
            ) from exc
    if not response.data or not response.data[0].embedding:
        raise DegradedCapabilityError(
            f"model {spec.model!r} returned no vector — not an embeddings model"
        )
    return len(response.data[0].embedding)
