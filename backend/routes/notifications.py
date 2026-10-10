"""The attention/notification surface — a durable record of what needs the operator's
notice, separate from the frozen per-run event stream (`runs/events/`), which dies
with its run. REST for backfill/read-state; its own SSE stream (the same framing as the
run transport — `id:` seq, flat JSON `data:`, ~15s keepalive comments, `Last-Event-ID`
resume) for live updates. Out-shapes are camelCase, like the app's other newer surfaces
(corpus/uploads).

*Whether* to notify — the emit policy of which run outcomes are noteworthy — is decided
by the callers that wire into `NotificationService.notify` (the engine's approval
parking, the run registry's terminal transitions, the approve/deny routes); this router
only exposes what the service already recorded.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel

from core.sse import parse_last_event_id, sse_stream
from routes import deps
from routes.camel import CamelModel
from routes.deps import OPERATOR_ID
from services.notifications import NotificationEvent, NotificationView

router = APIRouter(prefix="/notifications", tags=["notifications"])


class NotificationOut(CamelModel):
    id: str
    kind: str
    title: str
    body: str | None = None
    conversation_id: str | None = None
    run_id: str | None = None
    created_at: datetime
    read_at: datetime | None = None
    resolved_at: datetime | None = None


class NotificationListOut(CamelModel):
    items: list[NotificationOut]
    unread_count: int


class NotificationStreamEnvelope(CamelModel):
    """The SSE `data:` payload: `{type, seq, ts, notification}`."""

    type: Literal["notification.created", "notification.updated"]
    seq: int
    ts: datetime
    notification: NotificationOut


class MarkReadIn(BaseModel):
    ids: list[str]


class MarkReadOut(CamelModel):
    updated: int


def _out(view: NotificationView) -> NotificationOut:
    return NotificationOut(
        id=view.id,
        kind=view.kind,
        title=view.title,
        body=view.body,
        conversation_id=view.conversation_id,
        run_id=view.run_id,
        created_at=view.created_at,
        read_at=view.read_at,
        resolved_at=view.resolved_at,
    )


def _envelope(event: NotificationEvent) -> NotificationStreamEnvelope:
    return NotificationStreamEnvelope(
        type=f"notification.{event.kind}",  # "created" | "updated" -> the wire type
        seq=event.seq,
        ts=event.ts,
        notification=_out(event.notification),
    )


@router.get("", response_model=NotificationListOut)
async def list_notifications(
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    before: Annotated[datetime | None, Query()] = None,
    unread_only: bool = Query(default=False),
) -> NotificationListOut:
    items, unread_count = await deps.notifications(request).list_notifications(
        OPERATOR_ID, limit=limit, before=before, unread_only=unread_only
    )
    return NotificationListOut(items=[_out(v) for v in items], unread_count=unread_count)


@router.post("/read", response_model=MarkReadOut)
async def mark_read(body: MarkReadIn, request: Request) -> MarkReadOut:
    updated = await deps.notifications(request).mark_read(OPERATOR_ID, body.ids)
    return MarkReadOut(updated=updated)


@router.post("/read_all", response_model=MarkReadOut)
async def mark_all_read(request: Request) -> MarkReadOut:
    updated = await deps.notifications(request).mark_all_read(OPERATOR_ID)
    return MarkReadOut(updated=updated)


def _frame(event: NotificationEvent) -> str:
    """One notification as an SSE frame — the same framing the run transport emits, since
    the client parses both with one reader."""
    payload = _envelope(event).model_dump_json(by_alias=True)
    return f"id: {event.seq}\ndata: {payload}\n\n"


@router.get("/stream")
async def stream_notifications(request: Request, last_event_id: int | None = Query(default=None)):
    """SSE stream of `notification.created`/`notification.updated`. Reconnect with
    `Last-Event-ID` to replay from the in-memory ring buffer (process-lifetime)."""
    after = parse_last_event_id(request.headers.get("last-event-id"), last_event_id)
    service = deps.notifications(request)
    return sse_stream(lambda: service.subscribe(after), _frame)
