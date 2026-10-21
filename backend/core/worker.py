"""A reusable write-behind worker — the background-drainer substrate.

A single asyncio task drains a queue off the hot path. Failed items retry with
bounded exponential backoff; an item that exhausts its retries is handed to an
``on_drop`` callback so a terminal failure is **surfaced, not silently lost**.

Optionally **lock-aware**: a worker whose handler touches the encryption key
takes the vault's ``unlocked`` event and parks while the vault is locked,
resuming when it unlocks — rather than erroring on a missing key. A lock that
lands *mid-handler* is treated as a park, not a failed attempt, so an item is
never dropped for being in flight when the vault closed. While stopping, a
lock-parked worker stops waiting so shutdown never hangs.

This is the one place the "we own the queues" substrate lives. The conversation
persistence drainer is the first instance; the scheduler and notification
dispatch are meant to reuse it rather than re-implement queue + lifecycle +
retry + lock-awareness each time.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from core.backoff import backoff_delay
from core.config import get_settings
from core.tasks import STOP_TIMEOUT_S, cancel_and_wait

logger = logging.getLogger(__name__)


async def wait_unlocked_or_stop(unlocked: asyncio.Event | None, stopping: asyncio.Event) -> bool:
    """Park until ``unlocked`` is set or ``stopping`` is. True means the vault is open and
    the caller may proceed; False means it is stopping while still locked and should give up.

    The one spelling of the lock-aware park, shared by every lock-aware loop (this worker,
    the task scheduler) so they cannot disagree about what a lock mid-shutdown means.
    ``unlocked=None`` is a handler that never touches the key: always ready.
    """
    if unlocked is None or unlocked.is_set():
        return True
    stop_wait = asyncio.ensure_future(stopping.wait())
    unlock_wait = asyncio.ensure_future(unlocked.wait())
    try:
        _ = await asyncio.wait({stop_wait, unlock_wait}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        _ = stop_wait.cancel()
        _ = unlock_wait.cancel()
    return unlocked.is_set()


class WriteBehindWorker[T]:
    """Drains submitted items to ``handler`` on a background task.

    ``unlocked`` (the vault's event) gates processing on the vault being open —
    pass it only for handlers that touch the encryption key. ``on_drop`` is
    called with ``(item, exc)`` when an item exhausts ``max_attempts``.
    """

    def __init__(
        self,
        handler: Callable[[T], Awaitable[None]],
        *,
        name: str,
        unlocked: asyncio.Event | None = None,
        max_attempts: int = 4,
        base_backoff_s: float = 0.2,
        on_drop: Callable[[T, Exception], None] | None = None,
    ) -> None:
        self._handler: Callable[[T], Awaitable[None]] = handler
        self._name: str = name
        self._unlocked: asyncio.Event | None = unlocked
        self._max_attempts: int = max_attempts
        self._base_backoff_s: float = base_backoff_s
        self._on_drop: Callable[[T, Exception], None] | None = on_drop
        self._queue: asyncio.Queue[T] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None
        self._stopping: asyncio.Event = asyncio.Event()
        # Queued plus the one being processed, which `qsize()` no longer counts.
        self._unfinished: int = 0

    def submit(self, item: T) -> None:
        """Hot path: enqueue an item for background processing (never blocks)."""
        self._unfinished += 1
        self._queue.put_nowait(item)

    @property
    def pending(self) -> int:
        return self._queue.qsize()

    async def start(self) -> None:
        self._stopping.clear()
        self._task = asyncio.create_task(self._run(), name=self._name)

    async def join(self) -> None:
        """Block until every queued item has been processed (or dropped)."""
        await self._queue.join()

    async def stop(self, drain_timeout_s: float | None = None) -> None:
        """Flush what can be flushed — for at most ``drain_timeout_s`` — then cancel the
        drainer.

        When lock-gated and currently locked, processing is parked, so we don't
        block shutdown on ``join()`` — pending items stay unflushed. The flush is
        bounded (default ``Settings.shutdown_drain_timeout_s``): a handler stuck on a
        dead dependency must cost the shutdown seconds, not hang it. Items still queued
        when the deadline passes are logged, not silently lost.
        """
        self._stopping.set()
        timeout = drain_timeout_s
        if timeout is None:
            timeout = get_settings().shutdown_drain_timeout_s
        if self._unlocked is None or self._unlocked.is_set():
            try:
                await asyncio.wait_for(self._queue.join(), timeout)
            except TimeoutError:
                logger.warning(
                    "%s: %d item(s) unflushed after %.1fs; stopping anyway",
                    self._name,
                    self._queue.qsize(),
                    timeout,
                )
        elif self._unfinished:
            logger.warning(
                "%s: vault locked at stop; %d item(s) left unflushed",
                self._name,
                self._unfinished,
            )
        await cancel_and_wait(self._task, STOP_TIMEOUT_S)
        self._task = None

    async def _run(self) -> None:
        while True:
            item = await self._queue.get()
            try:
                await self._process(item)
            finally:
                self._unfinished -= 1
                self._queue.task_done()

    async def _ready(self) -> bool:
        """Wait until the vault is unlocked. False if stopping while still locked."""
        return await wait_unlocked_or_stop(self._unlocked, self._stopping)

    async def _process(self, item: T) -> None:
        """Handle one item: park while locked, retry transient errors, drop at the cap."""
        attempt = 0
        while True:
            # Park (without spending the retry budget) while the vault is locked;
            # give up only if we're shutting down before it ever unlocks.
            if not await self._ready():
                return
            try:
                await self._handler(item)
                return
            except Exception as exc:  # a bad item must not kill the worker
                # A lock that landed mid-handler isn't a failure — wait for unlock
                # and retry without consuming an attempt.
                if self._unlocked is not None and not self._unlocked.is_set():
                    continue
                attempt += 1
                if attempt >= self._max_attempts:
                    logger.exception("%s: dropping item after %d attempts", self._name, attempt)
                    if self._on_drop is not None:
                        self._on_drop(item, exc)
                    return
                await asyncio.sleep(backoff_delay(self._base_backoff_s, attempt))
