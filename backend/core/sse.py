"""Server-sent events: the pump every SSE surface in the app runs on.

There is more than one live stream here — a run's frozen event stream, the durable
notification feed — and they share a framing (``id:`` seq, one flat JSON ``data:`` line,
periodic comment keepalives, ``Last-Event-ID`` resume) and, more importantly, a shape of
*machinery*: relay the source through a bounded queue, put the keepalive timeout on the
queue rather than on the source, and cancel the relay cleanly on disconnect.

What differs between surfaces is only what they stream and how one item becomes a frame.
Those are the two arguments :func:`sse_stream` takes; everything else lives here once,
because a second copy of this loop is a second place for a subtly different disconnect
path to grow.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import suppress

from fastapi.responses import StreamingResponse

from core.shutdown import STOPPED, get_unless_shutdown
from core.tasks import STOP_TIMEOUT_S, cancel_and_wait

SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",  # disable proxy buffering so frames flush live
}

# Emit a comment frame when the source has been quiet this long, so a client can tell a
# legitimately quiet stream (a slow tool call) from a dead connection. Comment frames
# carry no ``data:`` line, so the client parser ignores them.
KEEPALIVE_INTERVAL_S = 15.0

# The relay queue between the pump and the response is bounded so a client that stops
# draining (a backgrounded tab) can't make it buffer the whole stream. When it fills, the
# pump blocks on ``put`` and stops draining the subscription, which the source then drops
# the same way it drops any wedged consumer — keeping the "never grow unbounded"
# invariant from ``runs/CLAUDE.md``.
RELAY_QUEUE_MAX = 256


def parse_last_event_id(header_value: str | None, query_value: int | None) -> int:
    """Resolve the resume point from the SSE header or an explicit query param."""
    if header_value:
        try:
            return max(0, int(header_value))
        except ValueError:
            pass
    if query_value is not None:
        return max(0, query_value)
    return 0


def sse_stream[T](
    subscribe: Callable[[], AsyncIterator[T]], frame: Callable[[T], str]
) -> StreamingResponse:
    """An SSE response relaying ``subscribe()``, rendering each item with ``frame``.

    ``subscribe`` is a factory rather than an iterator so the subscription is opened
    inside the response generator — i.e. when the client actually starts reading, and on
    the task that will own its cancellation.
    """

    async def frames() -> AsyncIterator[str]:
        # Relay through a local queue so the keepalive timeout sits on the queue, not on
        # the subscribe generator itself — cancelling the latter mid-await would
        # unsubscribe it. Cancelling the pump task on disconnect closes the subscription
        # cleanly through its own ``finally``.
        queue: asyncio.Queue[T | None] = asyncio.Queue(maxsize=RELAY_QUEUE_MAX)

        async def pump() -> None:
            try:
                async for item in subscribe():
                    await queue.put(item)
            finally:
                # Best-effort sentinel — never block here. On a full queue the consumer is
                # already gone (disconnect cancels this task), so a blocking put would just
                # deadlock the cancellation.
                with suppress(asyncio.QueueFull):
                    queue.put_nowait(None)

        task = asyncio.create_task(pump())
        try:
            while True:
                try:
                    # Races the process-wide shutdown signal: a stream with no end of its
                    # own (a feed, a parked run) must still let the server stop.
                    item = await get_unless_shutdown(queue, KEEPALIVE_INTERVAL_S)
                except TimeoutError:
                    yield ": ping\n\n"
                    continue
                if item is None or item is STOPPED:  # source ended, or the app is stopping
                    break
                yield frame(item)
        finally:
            await cancel_and_wait(task, STOP_TIMEOUT_S)

    return StreamingResponse(frames(), media_type="text/event-stream", headers=SSE_HEADERS)
