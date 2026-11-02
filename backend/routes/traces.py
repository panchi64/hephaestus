"""Read-only trace views: a conversation's turns, one run's spans, and background work."""

from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from models.trace_span import TraceSpan
from routes import deps
from runs.events import SpanCompleted
from services.traces import BackgroundActivity, TurnSummary

router = APIRouter(tags=["traces"])


def _span_view(row: TraceSpan) -> SpanCompleted:
    """A saved span in the live event's shape, so the client reads both the same way."""
    return SpanCompleted.model_validate(row, from_attributes=True)


@router.get(
    "/conversations/{conversation_id}/traces",
    response_model=list[TurnSummary],
    dependencies=[Depends(deps.private_gate)],
)
async def conversation_traces(request: Request, conversation_id: str) -> list[TurnSummary]:
    return await deps.traces(request).conversation_turns(conversation_id)


@router.get("/runs/{run_id}/trace", response_model=list[SpanCompleted])
async def run_trace(request: Request, run_id: str) -> list[SpanCompleted]:
    rows = await deps.traces(request).run_trace(run_id)
    if not rows:
        raise HTTPException(status_code=404, detail="no trace for this run")
    conversation_id = next((r.conversation_id for r in rows if r.conversation_id), None)
    if conversation_id is not None:
        await deps.require_private_open(request, conversation_id)
    return [_span_view(r) for r in rows]


@router.get("/traces/background", response_model=BackgroundActivity)
async def background_traces(
    request: Request, days: Annotated[int, Query(ge=1, le=30)] = 7
) -> BackgroundActivity:
    activity = await deps.traces(request).background(days)
    if deps.private_visible(request):
        return activity
    # Costs still count; only the link to a private thread is withheld.
    area = deps.private_area(request)
    linked = sorted({c.conversation_id for c in activity.recent if c.conversation_id})
    flags = await asyncio.gather(*(area.is_private(cid) for cid in linked))
    private = {cid for cid, flag in zip(linked, flags, strict=True) if flag}
    recent = [
        call.model_copy(update={"conversation_id": None})
        if call.conversation_id in private
        else call
        for call in activity.recent
    ]
    return activity.model_copy(update={"recent": recent})
