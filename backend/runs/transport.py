"""Pillar I/II — SSE transport: turn a Run's event stream into an HTTP response.

The only transport in v1 (SSE for server→client, POST for control). Disconnect
is safe — the generator just unsubscribes; the Run keeps executing and is fully
replayable on reconnect via ``Last-Event-ID``.

The pump itself — bounded relay queue, keepalive comments, clean cancellation — is
``core.sse``, shared with the notification feed. What belongs here is the one thing
specific to a run: which stream to subscribe to, and that an ``Event`` already knows how
to render itself as a frame.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable

from fastapi.responses import StreamingResponse

from core.sse import parse_last_event_id, sse_stream

from .events import Event
from .run import Run

__all__ = ["parse_last_event_id", "sse_response"]


def sse_response(
    run: Run,
    after_seq: int = 0,
    *,
    watching: bool = True,
    allowed: Callable[[], bool] | None = None,
) -> StreamingResponse:
    """The run's events as SSE. ``allowed`` is asked before each event; the stream ends at
    the first refusal, so access lost mid-stream stops it."""

    async def events() -> AsyncIterator[Event]:
        async for event in run.stream.subscribe(after_seq, watching=watching):
            if allowed is not None and not allowed():
                return
            yield event

    return sse_stream(events, lambda event: event.sse())
