"""The process-wide "we are shutting down" signal — what lets a long-lived response end.

A server cannot finish stopping until its open connections close, and the streams this app
serves (the notification feed, the event stream of a run parked on an approval, a proxied
preview) have no end of their own. The code that would close them sits in the lifespan's
``finally`` — which uvicorn runs only *after* the connections are gone. Each side waited on
the other, a second Ctrl+C force-exited past the lifespan, and the write-behind queues,
the VM and proxies it was meant to stop were simply abandoned.

This module breaks the cycle by being raised from *outside* the lifespan: the uvicorn
server in :mod:`core.server` calls :func:`begin_shutdown` the instant it receives the
signal, and the lifespan calls it again as its first step (a test client has no signal).
Every long-lived response races its waiting against :func:`wait_shutdown` and ends cleanly,
which is what lets the server finish and the lifespan run.

It is module state rather than ``app.state`` because its readers are the streaming helpers
in ``core/`` and ``runs/``, which are handed no app — and because the signal is a property
of the *process*, which one uvicorn server (and so one app) owns. The event itself is
per-loop: an ``asyncio.Event`` belongs to the loop that waits on it, and the signal arrives
on whichever thread uvicorn's handler runs, so setting one goes through
``call_soon_threadsafe``. The lifespan resets the flag once shutdown is complete so a second
app in the same process (a test) starts clear.
"""

from __future__ import annotations

import asyncio
import contextlib
import weakref
from collections.abc import AsyncIterator, Awaitable
from typing import Final


class _Stopped:
    """The type of :data:`STOPPED` — what a shutdown-aware wait returns instead of a value."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "STOPPED"


STOPPED: Final = _Stopped()

_flag = False
# One event per loop that has ever waited. Weak keys, so a finished test loop is not kept.
_events: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Event] = (
    weakref.WeakKeyDictionary()
)


def is_shutting_down() -> bool:
    return _flag


def begin_shutdown() -> None:
    """Raise the signal. Safe from any thread and from a signal handler; idempotent."""
    global _flag
    _flag = True
    for loop, event in list(_events.items()):
        with contextlib.suppress(RuntimeError):  # the loop already closed
            loop.call_soon_threadsafe(event.set)


def reset_shutdown() -> None:
    """Clear the signal once shutdown has finished, so the next app in this process starts
    clear. Called by the lifespan as its last step, never mid-shutdown."""
    global _flag
    _flag = False
    _events.clear()


async def wait_shutdown() -> None:
    """Return once shutdown has begun (immediately if it already has)."""
    if _flag:
        return
    loop = asyncio.get_running_loop()
    event = _events.get(loop)
    if event is None:
        event = _events[loop] = asyncio.Event()
    # Re-check: the flag may have been raised between the first check and registration.
    if _flag:
        event.set()
    await event.wait()


async def _first[T](aw: Awaitable[T], timeout: float | None) -> T | _Stopped:
    """Await ``aw`` unless shutdown begins first. ``TimeoutError`` after ``timeout``."""
    if _flag:
        close = getattr(aw, "close", None)
        if close is not None:
            close()  # a never-awaited coroutine would warn
        return STOPPED
    work = asyncio.ensure_future(aw)
    stop = asyncio.ensure_future(wait_shutdown())
    try:
        done, _ = await asyncio.wait(
            {work, stop}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
        )
    finally:
        for fut in (work, stop):
            if not fut.done():
                fut.cancel()
        # Reap the loser so it never warns as destroyed-while-pending.
        await asyncio.gather(work, stop, return_exceptions=True)
    if work in done:
        return work.result()
    if stop in done:
        return STOPPED
    raise TimeoutError


async def get_unless_shutdown[T](
    queue: asyncio.Queue[T], timeout: float | None = None
) -> T | _Stopped:
    """``queue.get()``, or :data:`STOPPED` once shutdown begins. Raises ``TimeoutError``
    after ``timeout`` seconds of neither — the keepalive tick of an SSE pump."""
    return await _first(queue.get(), timeout)


async def ends_on_shutdown[T](source: AsyncIterator[T]) -> AsyncIterator[T]:
    """Relay ``source`` until it ends or shutdown begins, then close it.

    For a stream the app does not author (a proxied upstream body), where the wait cannot be
    threaded through the source itself.
    """
    try:
        while True:
            try:
                item = await _first(anext(source), None)
            except StopAsyncIteration:
                return
            if item is STOPPED:
                return
            yield item  # type: ignore[misc]
    finally:
        aclose = getattr(source, "aclose", None)
        if aclose is not None:
            with contextlib.suppress(Exception):
                await aclose()
