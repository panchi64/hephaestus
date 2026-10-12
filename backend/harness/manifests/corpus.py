"""The knowledge corpus — one retrieval index fed by many source adapters.

The rich stores (memory, cross-chat search) plug in untouched as wrapper adapters;
chunked content lands in the generic chunk store (the folder source here; other
surfaces register their own adapters from their own manifests — `CorpusIndex.register`
is the seam, so a new source never edits this file). The folder indexer is
lock-aware (parks while the vault is locked).
"""

from __future__ import annotations

from core.api_scopes import ScopeClaim
from harness.manifest import FeatureManifest, FeatureRuntime, HarnessContext
from routes import corpus as corpus_routes
from services.conversation_search import ConversationSearch
from services.corpus import (
    ConversationAdapter,
    CorpusChunkStore,
    CorpusIndex,
    FolderAdapter,
    MemoryAdapter,
    record_adapters,
)
from services.embeddings import RegistryEmbedder
from services.memory import MemoryStore
from services.registry import ModelRegistry
from tools.corpus import corpus_toolset


async def _build(ctx: HarnessContext) -> FeatureRuntime:
    embedder = ctx.services.get(RegistryEmbedder)
    chunk_store = CorpusChunkStore(ctx.engine, ctx.vault, embedder)
    folder_adapter = FolderAdapter(ctx.engine, chunk_store, ctx.vault.unlocked_event)
    index = CorpusIndex(embedder, ctx.services.get(ModelRegistry), chunk_store, folder_adapter)
    index.register(folder_adapter)
    index.register(MemoryAdapter(ctx.services.get(MemoryStore)))
    index.register(ConversationAdapter(ctx.services.get(ConversationSearch)))
    for adapter in record_adapters(ctx.engine, ctx.vault):
        index.register(adapter)
    await ctx.lifecycle.start(
        "corpus-folder", start=folder_adapter.start, stop=folder_adapter.stop
    )
    return FeatureRuntime(
        services=(chunk_store, index, folder_adapter),
        capabilities=(index,),
        state={
            "corpus": index,
            "corpus_folder": folder_adapter,
            "corpus_chunk_store": chunk_store,
        },
    )


MANIFEST = FeatureManifest(
    name="corpus",
    after=("memory", "conversation-search"),
    routers=(corpus_routes.router,),
    api_scopes=(ScopeClaim("knowledge", ("/corpus",)),),
    toolsets=(("corpus", corpus_toolset),),
    build=_build,
)
