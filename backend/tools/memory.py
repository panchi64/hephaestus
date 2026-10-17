"""Memory tools — the agent's thin adapter over the memory capability.

Memory works like the skill catalog: pinned memories reach every turn's instructions as
``id: description`` lines (:func:`memory_catalog_instructions`), ``open`` returns one
memory's full text, and ``recall`` searches the rest. Every verb is a pass-through to
:class:`~services.memory.MemoryStore` reached via ``RunDeps``, the same service the REST
routes call.

If memory isn't wired into the run, the tools say so rather than failing — the model
adapts (graceful degradation).
"""

from __future__ import annotations

from cryptography.exceptions import InvalidTag
from pydantic_ai import FunctionToolset, ModelRetry, RunContext

from core.diagnostics import render_tool_error
from core.exceptions import NotFoundError
from core.vault import VaultLocked
from prompts.memory import MEMORY_CATALOG, MEMORY_OTHERS, MEMORY_PINNED_LIMIT
from services.memory import MemoryCatalog, MemoryStore, MemoryView

from .deps import RunDeps

_UNAVAILABLE = "Memory is unavailable."


def _unknown(memory_id: str) -> ModelRetry:
    return ModelRetry(
        render_tool_error(
            f"no memory with id {memory_id!r}",
            hints=[
                "use an id from the pinned list in your instructions or from a "
                + "`memory_recall` result, exactly as given"
            ],
        )
    )


def _entry(view: MemoryView) -> dict[str, object]:
    return {
        "id": view.id,
        "description": view.description,
        "content": view.content,
        "pinned": view.pinned,
    }


def memory_toolset() -> FunctionToolset[RunDeps]:
    toolset: FunctionToolset[RunDeps] = FunctionToolset()

    @toolset.tool
    async def remember(
        ctx: RunContext[RunDeps], content: str, description: str, pinned: bool = False
    ) -> str:
        """Save something to long-term memory for future conversations.

        Use it when the operator tells you something about themselves, their setup or how
        they want you to work that will still matter next time. If memory already holds
        it, change that memory with `memory_update` instead of saving a second.

        ``content`` must make sense with no conversation around it. ``description`` is
        one short line naming what it is ("How the operator wants to be addressed"); it
        is the index entry you see instead of the content.

        Set ``pinned`` for what should shape every conversation from the first reply:
        how to address the operator, standing preferences, facts about their setup you
        would otherwise get wrong. Pins are listed in your instructions every turn, so
        pin sparingly; topic-specific facts stay unpinned for `memory_recall`."""
        store = ctx.deps.caps.get_optional(MemoryStore)
        if store is None:
            return _UNAVAILABLE
        view = await store.remember(
            ctx.deps.owner_id, content, description=description, pinned=pinned
        )
        return f"Remembered (id {view.id}{', pinned' if view.pinned else ''})."

    @toolset.tool
    async def open(ctx: RunContext[RunDeps], id: str) -> dict[str, object]:
        """Read one memory in full, by an ``id`` from your pinned list or a `memory_recall`
        hit. Open it when the task depends on details its description only names."""
        store = ctx.deps.caps.get_optional(MemoryStore)
        if store is None:
            return {"error": _UNAVAILABLE}
        try:
            view = await store.get(ctx.deps.owner_id, id)
        except NotFoundError:
            raise _unknown(id) from None
        return _entry(view)

    @toolset.tool
    async def update(
        ctx: RunContext[RunDeps],
        id: str,
        content: str | None = None,
        description: str | None = None,
        pinned: bool | None = None,
    ) -> dict[str, object]:
        """Correct, re-describe, pin or unpin a memory.

        Use it when the operator revises something memory holds ("call me Fran, not
        Francisco"), so the old version stops steering you. Pass only what changes;
        ``content`` replaces the whole text. Pin by `memory_remember`'s rule."""
        store = ctx.deps.caps.get_optional(MemoryStore)
        if store is None:
            return {"error": _UNAVAILABLE}
        try:
            view = await store.update(
                ctx.deps.owner_id, id, content=content, description=description, pinned=pinned
            )
        except NotFoundError:
            raise _unknown(id) from None
        return _entry(view)

    @toolset.tool
    async def recall(
        ctx: RunContext[RunDeps], query: str, limit: int = 5
    ) -> list[dict[str, object]]:
        """Search long-term memory by meaning, with a keyword fallback, when the
        conversation touches something the operator may have told you before. Hits come
        best first with ``matched_by`` (``semantic``, ``keyword`` or ``both``)."""
        store = ctx.deps.caps.get_optional(MemoryStore)
        if store is None:
            return [{"error": _UNAVAILABLE}]
        hits = await store.recall(ctx.deps.owner_id, query, limit=limit)
        return [
            {
                "id": h.memory.id,
                "description": h.memory.description,
                "content": h.memory.content,
                "matched_by": h.matched_by,
            }
            for h in hits
        ]

    return toolset


def memory_catalog_block(catalog: MemoryCatalog) -> str:
    """The pinned-memory index, or ``""`` when nothing is pinned."""
    if not catalog.pinned:
        return ""
    entries = "\n".join(f"- {memory_id}: {text}" for memory_id, text in catalog.pinned)
    others = (
        MEMORY_OTHERS.format(
            count=f"{catalog.others} more memor{'y' if catalog.others == 1 else 'ies'}"
        )
        if catalog.others
        else ""
    )
    return MEMORY_CATALOG.format(entries=entries, others=others)


async def memory_catalog_instructions(ctx: RunContext[RunDeps]) -> str:
    """List the pinned memories (`MEM-4`) as a dynamic instruction, current every turn and
    kept out of history. Empty while the vault is locked: the descriptions are sealed."""
    store = ctx.deps.caps.get_optional(MemoryStore)
    if store is None:
        return ""
    try:
        catalog = await store.catalog(ctx.deps.owner_id, limit=MEMORY_PINNED_LIMIT)
    except VaultLocked, InvalidTag:
        return ""
    return memory_catalog_block(catalog)
