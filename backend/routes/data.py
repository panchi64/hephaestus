"""Bulk clears of the operator's data, one kind at a time."""

from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from routes import conversation_teardown as teardown
from routes import deps
from routes.deps import OPERATOR_ID
from services.conversations import ConversationSummaryView

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/data", tags=["data"])

DataKind = Literal["chats", "memories", "notifications", "uploads"]


class ClearResult(BaseModel):
    deleted: int
    # Chats a live run holds; deleting one would tear the tree out from beneath the run.
    skipped_ids: list[str] = []


def _root_id(summary: ConversationSummaryView, by_id: dict[str, ConversationSummaryView]) -> str:
    """The top-level thread a sub-agent thread hangs under, as far as the listing reaches."""
    while summary.parent_id is not None and summary.parent_id in by_id:
        summary = by_id[summary.parent_id]
    return summary.id


async def _try_delete(request: Request, conversation_id: str, attached: set[str]) -> bool:
    """Delete one thread for a bulk clear. False when a live run holds it; True when it is
    gone, including when it already went with its parent."""
    try:
        attached |= await teardown.delete_conversation(
            request, conversation_id, discard_branch=True, purge_images=False
        )
    except HTTPException as exc:
        if exc.status_code == 409:
            return False
        if exc.status_code != 404:
            raise
    return True


async def _clear_chats(request: Request) -> ClearResult:
    summaries = await deps.store(request).list_conversations(OPERATOR_ID)
    by_id = {summary.id: summary for summary in summaries}
    attached: set[str] = set()
    deleted = 0
    skipped: set[str] = set()
    try:
        # Top-level threads first: each takes its sub-agent threads with it.
        for summary in summaries:
            if summary.parent_id is not None:
                continue
            if await _try_delete(request, summary.id, attached):
                deleted += 1
            else:
                skipped.add(summary.id)
        # What is left hangs under a skipped thread, whose live run still uses it, or under
        # a parent that is already gone.
        for summary in summaries:
            if summary.parent_id is not None and _root_id(summary, by_id) not in skipped:
                _ = await _try_delete(request, summary.id, attached)
    finally:
        await teardown.purge_unreferenced_images(request, attached)
    return ClearResult(deleted=deleted, skipped_ids=sorted(skipped))


async def _clear_uploads(request: Request) -> ClearResult:
    ids = await deps.uploads(request).delete_all(OPERATOR_ID)
    await deps.store(request).detach_uploads(OPERATOR_ID, ids)
    return ClearResult(deleted=len(ids))


@router.post("/clear/{kind}", response_model=ClearResult)
async def clear(kind: DataKind, request: Request) -> ClearResult:
    """Permanently delete every item of one kind. Chats go with their images and everything
    kept for them; a chat with a live run is skipped and returned in ``skipped_ids``."""
    match kind:
        case "chats":
            result = await _clear_chats(request)
        case "memories":
            result = ClearResult(deleted=await deps.memory(request).delete_all(OPERATOR_ID))
        case "notifications":
            result = ClearResult(deleted=await deps.notifications(request).delete_all(OPERATOR_ID))
        case "uploads":
            result = await _clear_uploads(request)
    logger.info("cleared %s: %d deleted, %d skipped", kind, result.deleted, len(result.skipped_ids))
    return result
