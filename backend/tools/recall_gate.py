"""The context-control gate for global recall (AE-3.8).

Global, relevance-ranked recall over indexed folders, uploads and past conversations
pulls content the operator did not write into the model's context, so it pauses for
approval: a denial keeps that content out. ``platform_search`` raises it unless the
search stays within memory and the catalogs.

``memory_recall`` is deliberately **not** gated. Long-term memory holds only what
this agent was told to remember on the operator's behalf — their own notes, never a
document someone else wrote — so there is nothing there to keep out, and gating it bought
a park on the one lookup the agent needs most often to answer in the operator's own terms.

An explicit-id read — a source the operator already referenced, e.g. a file attached to
the turn, or one hit read by ref after a search — is *not* global recall and passes
through ungated, except a read of a private thread, which ``gate_private_read`` asks about
every time.

The gate raises ``ApprovalRequired``, which only *defers* (rather than erroring) when the
composing agent's ``output_type`` admits ``DeferredToolRequests`` — as the chat engine's
agent does. A recall toolset must only be composed into such an agent.
"""

from __future__ import annotations

from pydantic_ai import ModelRetry, RunContext
from pydantic_ai.exceptions import ApprovalRequired

from services.permissions.guards import GUARD_KEY
from services.private_area import PrivateArea

from .deps import RunDeps

#: The corpus surface of past conversations, the only one a private read can reach.
CONVERSATIONS_SURFACE = "surf-conversations"


async def gate_private_read(ctx: RunContext[RunDeps], ref: str, purpose: str | None) -> bool:
    """Ask the operator before a private thread is read, every time, even with the area
    open. Returns whether the read may include a private thread.

    False lets the corpus refuse it as missing, which is also the answer while the area is
    locked or without a :class:`PrivateArea` to ask. The guard names the asking thread and
    the purpose, never the private thread, since the card outlives the area being open.
    """
    source_id, _, item = ref.partition(":")
    area = ctx.deps.caps.get_optional(PrivateArea)
    if (
        source_id != CONVERSATIONS_SURFACE
        or area is None
        or not area.is_unlocked()
        or not await area.is_private(item)
    ):
        return False
    if not purpose or not purpose.strip():
        raise ModelRetry(
            "This is a private chat. Call `platform_read` again with `purpose`: one sentence "
            + "on what you need from it and why. The operator sees it when asked to approve."
        )
    if not ctx.tool_call_approved:
        asker = ctx.deps.conversation_id
        caller = (await area.title(ctx.deps.owner_id, asker) if asker else None) or "a chat"
        raise ApprovalRequired(
            metadata={
                GUARD_KEY: "Reads one of your private chats. Private chats always ask, and "
                + "no standing approval covers them.",
                "explanation": f'Asked from "{caller}": {purpose.strip()}',
            }
        )
    area.touch()
    return True


def gate_global_recall(ctx: RunContext[RunDeps]) -> None:
    """Pause a global recall for operator approval unless it was already approved.
    ``tool_call_approved`` is set on the re-invocation after an approval, so this raises
    once and then lets the recall run."""
    if not ctx.tool_call_approved:
        raise ApprovalRequired()
