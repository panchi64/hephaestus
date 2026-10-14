"""Memory tools — the agent's thin adapter over the memory capability.

Two verbs, both thin pass-throughs to :class:`~services.memory.MemoryStore`
reached via ``RunDeps`` (no logic here — `MEM-*` lives in the service, the same
one the REST routes call). Recall returns each hit's text plus how it surfaced
(semantic / keyword / both / pinned) so the model can weigh it.

If memory isn't wired into the run, the tools say so rather than failing — the
model adapts (graceful degradation).
"""

from __future__ import annotations

from pydantic_ai import FunctionToolset, RunContext

from services.memory import MemoryStore

from .deps import RunDeps


def memory_toolset() -> FunctionToolset[RunDeps]:
    toolset: FunctionToolset[RunDeps] = FunctionToolset()

    @toolset.tool
    async def remember(ctx: RunContext[RunDeps], content: str) -> str:
        """Save a fact or preference to long-term memory for future recall."""
        store = ctx.deps.caps.get_optional(MemoryStore)
        if store is None:
            return "Memory is unavailable."
        view = await store.remember(ctx.deps.owner_id, content)
        return f"Remembered (id {view.id})."

    @toolset.tool
    async def recall(
        ctx: RunContext[RunDeps], query: str, limit: int = 5
    ) -> list[dict[str, object]]:
        """Recall relevant memories by meaning (with keyword fallback)."""
        store = ctx.deps.caps.get_optional(MemoryStore)
        if store is None:
            return [{"error": "Memory is unavailable."}]
        hits = await store.recall(ctx.deps.owner_id, query, limit=limit)
        return [{"content": h.memory.content, "matched_by": h.matched_by} for h in hits]

    return toolset
