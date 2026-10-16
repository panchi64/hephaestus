"""Long-term memory (`MEM-*`) — hybrid recall over an encrypted store.

Embeds via the shared registry embedder; degrades to keyword recall when no
embedding endpoint is configured.
"""

from __future__ import annotations

from core.api_scopes import ScopeClaim
from harness.manifest import FeatureManifest, FeatureRuntime, HarnessContext
from routes import memory as memory_routes
from services.embeddings import RegistryEmbedder
from services.memory import MemoryStore
from tools.memory import memory_catalog_instructions, memory_toolset


async def _build(ctx: HarnessContext) -> FeatureRuntime:
    memory = MemoryStore(ctx.engine, ctx.vault, ctx.services.get(RegistryEmbedder))
    return FeatureRuntime(services=(memory,), capabilities=(memory,), state={"memory": memory})


MANIFEST = FeatureManifest(
    name="memory",
    routers=(memory_routes.router,),
    api_scopes=(ScopeClaim("memory", ("/memory",)),),
    toolsets=(("memory", memory_toolset),),
    # Every turn lists the pinned memories (ids + descriptions only).
    instructions=(memory_catalog_instructions,),
    build=_build,
)
