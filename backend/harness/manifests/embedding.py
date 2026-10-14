"""Embedding-space healing — the reindexer and the boot-time backfill.

`EMB-2` segregates vectors by the model that produced them, so changing the embedding
endpoint strands every vector written under the old one, and content persisted before any
embedder existed has no vector at all. Two things close that gap: the `EmbeddingReindexer`
re-embeds on demand (with progress the operator can watch), and `_backfill_embeddings`
lifts whatever backlog exists once the vault is unlocked.

This lived beside local model serving while the app served its own models, because binding
a freshly served embedding model was the most common way to trigger a heal. Serving is
gone — Hephaestus now speaks only to endpoints the operator points it at — but the healing is
not about *where* the embedder runs, only about the model changing underneath the vectors.
So it stands on its own here.
"""

from __future__ import annotations

import logging

from core.vault import Vault
from harness.manifest import FeatureManifest, FeatureRuntime, HarnessContext
from routes.deps import OPERATOR_ID
from services.conversations import ConversationStore
from services.corpus import CorpusChunkStore
from services.memory import MemoryStore
from services.registry import ModelRegistry
from services.reindex import EmbeddingReindexer

logger = logging.getLogger(__name__)


async def _backfill_embeddings(
    conversations: ConversationStore,
    memory: MemoryStore,
    chunk_store: CorpusChunkStore,
    vault: Vault,
) -> None:
    """Best-effort: once the vault is unlocked, embed any conversation messages,
    memories, AND corpus chunks that have no vector yet (e.g. persisted before an
    embedding endpoint existed) so semantic recall covers the backlog, not just new
    content. Runs in the background, waits for unlock so it never touches sealed
    data, and degrades to a no-op when no embedder is configured. Every store is
    lifted symmetrically."""
    _ = await vault.unlocked_event.wait()
    try:
        count = await conversations.backfill_embeddings(OPERATOR_ID)
        if count:
            logger.info("conversation search: backfilled %d message embeddings", count)
    except Exception:
        logger.exception("conversation search: embedding backfill failed")
    try:
        count = await memory.reembed(OPERATOR_ID)
        if count:
            logger.info("memory: backfilled %d memory embeddings", count)
    except Exception:
        logger.exception("memory: embedding backfill failed")
    try:
        count = await chunk_store.reembed(OPERATOR_ID)
        if count:
            logger.info("corpus: backfilled %d chunk embeddings", count)
    except Exception:
        logger.exception("corpus: embedding backfill failed")


async def _build(ctx: HarnessContext) -> FeatureRuntime:
    registry = ctx.services.get(ModelRegistry)
    conversations = ctx.services.get(ConversationStore)
    memory = ctx.services.get(MemoryStore)
    chunk_store = ctx.services.get(CorpusChunkStore)
    # Heals semantic recall after the operator changes the embedding model: EMB-2
    # segregates vectors by model, so a swap strands every existing vector until
    # it's re-embedded. Runs the reindex in the background and exposes progress.
    reindexer = EmbeddingReindexer(registry, memory, conversations, chunk_store)
    ctx.lifecycle.on_stop("embedding-reindexer", reindexer.shutdown)
    # Lift any pre-existing backlog (messages + memories + corpus chunks) into the
    # semantic index once unlocked — off the critical path; new content is already
    # embedded as it persists.
    _ = ctx.lifecycle.track(
        "embedding-backfill",
        _backfill_embeddings(conversations, memory, chunk_store, ctx.vault),
    )
    return FeatureRuntime(
        services=(reindexer,),
        state={"embedding_reindexer": reindexer},
    )


MANIFEST = FeatureManifest(
    name="embedding",
    after=("corpus", "memory"),
    build=_build,
)
