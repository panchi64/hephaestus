"""Re-embed memories, the cross-chat index, and the corpus when the embedding model
changes.

Changing the ``embedding`` role strands every existing vector: EMB-2 segregates by
model, so prior memories, chat messages, and corpus chunks drop to keyword-only recall
until they're re-embedded into the new space. This coordinator runs that heal in the
background — off the request path — and exposes a small status so the UI can show
progress and signal that recall is partially degraded until it finishes.

A single reindex runs at a time per process; a fresh trigger supersedes an in-flight
one (the latest embedding model wins). It is best-effort: a degraded embedder or a
failure leaves vectors as they are, to be lifted by the next trigger or startup
backfill.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from core.exceptions import DegradedCapabilityError, NotFoundError
from core.tasks import STOP_TIMEOUT_S, cancel_and_wait
from services.conversations import ConversationStore
from services.corpus.chunk_store import CorpusChunkStore
from services.memory import MemoryStore
from services.registry import ModelRegistry

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReindexStatus:
    """A snapshot the UI renders: what the last/current reindex is doing."""

    state: str  # idle | running | done | degraded | error
    memories: int  # memories re-embedded in the current/last run
    messages: int  # chat messages re-embedded in the current/last run
    chunks: int = 0  # corpus chunks re-embedded in the current/last run
    detail: str | None = None
    completed_at: datetime | None = None


class EmbeddingReindexer:
    def __init__(
        self,
        registry: ModelRegistry,
        memory: MemoryStore,
        conversations: ConversationStore,
        chunks: CorpusChunkStore,
    ) -> None:
        self._registry = registry
        self._memory = memory
        self._conversations = conversations
        self._chunks = chunks
        self._status = ReindexStatus(state="idle", memories=0, messages=0)
        self._task: asyncio.Task | None = None

    def status(self) -> ReindexStatus:
        return self._status

    def trigger(self, owner_id: str) -> None:
        """Start (or restart) a background reindex. A new trigger supersedes an
        in-flight one so the most recently chosen embedding model is the one we
        converge on."""
        if self._task is not None and not self._task.done():
            self._task.cancel()
        # Reflect "running" synchronously so the POST response (and an immediate
        # status poll) shows the just-started run, not the prior terminal state.
        self._status = ReindexStatus(state="running", memories=0, messages=0)
        self._task = asyncio.create_task(self._run(owner_id))

    async def shutdown(self) -> None:
        await cancel_and_wait(self._task, STOP_TIMEOUT_S)

    async def _run(self, owner_id: str) -> None:
        self._status = ReindexStatus(state="running", memories=0, messages=0)
        try:
            spec = await self._registry.resolve_embedding_spec(owner_id)
        except DegradedCapabilityError, NotFoundError:
            # No embedding model, or its endpoint was deleted out from under us —
            # a terminal, non-"running" state so the UI stops polling/spinning.
            self._status = ReindexStatus(
                state="degraded",
                memories=0,
                messages=0,
                detail="no embedding model configured",
            )
            return
        try:
            memories = await self._memory.reembed(owner_id, current_model=spec.model)
            messages = await self._conversations.reindex_embeddings(
                owner_id, current_model=spec.model
            )
            chunks = await self._chunks.reembed(owner_id, current_model=spec.model)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("embedding reindex failed")
            self._status = ReindexStatus(
                state="error", memories=0, messages=0, detail="reindex failed"
            )
            return
        self._status = ReindexStatus(
            state="done",
            memories=memories,
            messages=messages,
            chunks=chunks,
            completed_at=datetime.now(UTC),
        )
        logger.info(
            "embedding reindex: re-embedded %d memories, %d messages, %d chunks",
            memories,
            messages,
            chunks,
        )
