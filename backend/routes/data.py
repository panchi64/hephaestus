"""Bulk clears of the operator's data, one kind at a time."""

from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel

from routes import deps
from routes.deps import OPERATOR_ID

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/data", tags=["data"])

DataKind = Literal["chats", "memories", "notifications", "uploads"]


class ClearResult(BaseModel):
    deleted: int
    # Chats a live run holds; deleting one would tear the tree out from beneath the run.
    skipped_ids: list[str] = []


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
            outcome = await deps.conversation_teardown(request).clear_all(OPERATOR_ID)
            result = ClearResult(deleted=outcome.deleted, skipped_ids=outcome.skipped_ids)
        case "memories":
            result = ClearResult(deleted=await deps.memory(request).delete_all(OPERATOR_ID))
        case "notifications":
            result = ClearResult(deleted=await deps.notifications(request).delete_all(OPERATOR_ID))
        case "uploads":
            result = await _clear_uploads(request)
    logger.info("cleared %s: %d deleted, %d skipped", kind, result.deleted, len(result.skipped_ids))
    return result
