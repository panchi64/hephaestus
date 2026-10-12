"""Cross-chat search — hybrid recall over the operator's other conversations plus a
transcript read, reached by the agent through ``platform_search``."""

from __future__ import annotations

from harness.manifest import FeatureManifest, FeatureRuntime, HarnessContext
from services.conversation_search import ConversationSearch
from services.conversations import ConversationStore
from services.embeddings import RegistryEmbedder


async def _build(ctx: HarnessContext) -> FeatureRuntime:
    search = ConversationSearch(
        ctx.engine,
        ctx.vault,
        ctx.services.get(RegistryEmbedder),
        ctx.services.get(ConversationStore),
    )
    return FeatureRuntime(
        services=(search,), capabilities=(search,), state={"conversation_search": search}
    )


MANIFEST = FeatureManifest(
    name="conversation-search",
    build=_build,
)
