"""Pillar I — the per-run event stream: in-memory buffer + broker.

One :class:`RunStream` per Run owns the sequence-numbered event buffer and the
set of live subscribers. It does two jobs at once:

- **buffer** — every event is appended in ``seq`` order and kept for the run's
  lifetime, so a client that disconnects and reconnects can replay what it
  missed on reconnect.
- **broker** — each emit fans out to every live subscriber's queue.

The buffer is in-memory and dies with the process, which the spec explicitly
licenses ("continuity need not survive a server restart").

Correctness note: ``emit`` is synchronous and ``subscribe`` does its
register-then-snapshot with no ``await`` in between. Under single-threaded
asyncio that window is atomic, so every event lands in exactly one of {backlog,
live queue} — no gap, no duplicate, for any subscriber at any time.

Backpressure: each subscriber queue is bounded. A consumer that stops draining
(a stalled or wedged client) is dropped rather than allowed to grow without
limit — it can reconnect with ``Last-Event-ID`` and replay the gap from the
buffer, so dropping it is lossless, not data loss.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from core.shutdown import STOPPED, get_unless_shutdown

from .events import Event, EventBody, now_utc

# Per-subscriber backlog cap before we treat the consumer as wedged and drop it.
SUBSCRIBER_QUEUE_MAX = 1024


class RunStream:
    def __init__(self) -> None:
        self._buffer: list[Event] = []
        self._seq: int = 0
        self._subscribers: set[asyncio.Queue[Event | None]] = set()
        self._closed: bool = False

    @property
    def last_seq(self) -> int:
        return self._seq

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def subscriber_count(self) -> int:
        """How many live subscribers are attached right now — the run-terminal emit
        policy's "was anyone watching" check (see ``RunRegistry``'s injected
        ``on_terminal`` hook). Read this *before* ``close()``: closing signals every
        subscriber's queue but doesn't discard them itself — each subscriber's own
        generator removes itself once it wakes to the sentinel, so a count taken after
        ``close()`` can race that cleanup."""
        return len(self._subscribers)

    def emit(self, body: EventBody) -> Event:
        """Stamp, buffer, and fan out an event. Synchronous and atomic."""
        if self._closed:
            raise RuntimeError("emit on a closed RunStream")
        self._seq += 1
        event = Event(seq=self._seq, ts=now_utc(), body=body)
        self._buffer.append(event)
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                self._drop_slow(queue)
        return event

    def _drop_slow(self, queue: asyncio.Queue[Event | None]) -> None:
        """Evict a subscriber that isn't keeping up. Its connection ends; the
        client can reconnect and replay the missed events from the buffer."""
        self._subscribers.discard(queue)
        while not queue.empty():  # make room for the close sentinel
            _ = queue.get_nowait()
        queue.put_nowait(None)

    def replay(self, after_seq: int = 0) -> list[Event]:
        """Buffered events with ``seq > after_seq`` (all of them if 0)."""
        if after_seq <= 0:
            return list(self._buffer)
        return [e for e in self._buffer if e.seq > after_seq]

    async def subscribe(self, after_seq: int = 0) -> AsyncIterator[Event]:
        """Replay missed events (``seq > after_seq``) then stream live ones.

        Ends when the stream is closed (the run reached a terminal state) or the app
        begins shutting down — a run parked on an approval has no terminal state coming,
        and its open stream would otherwise keep the server from stopping.
        Subscribing to an already-closed stream replays the backlog and stops —
        so a client reconnecting after the run ended still gets the full record.
        """
        queue: asyncio.Queue[Event | None] = asyncio.Queue(maxsize=SUBSCRIBER_QUEUE_MAX)
        # --- atomic: register before snapshot, no await between ---
        self._subscribers.add(queue)
        backlog = self.replay(after_seq)
        already_closed = self._closed
        # ----------------------------------------------------------
        try:
            for event in backlog:
                yield event
            if already_closed:
                return
            while True:
                event = await get_unless_shutdown(queue)
                if event is None or event is STOPPED:  # run closed, or the app is stopping
                    return
                yield event
        finally:
            self._subscribers.discard(queue)

    def close(self) -> None:
        """Mark terminal and signal every live subscriber to finish."""
        if self._closed:
            return
        self._closed = True
        for queue in list(self._subscribers):
            if queue.full():  # a wedged consumer — drain to make room for the sentinel
                self._drop_slow(queue)
            else:
                queue.put_nowait(None)
