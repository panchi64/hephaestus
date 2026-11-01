"""Holds a watched park's notice, and sends it once the operator has left the run's chat."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from core.tasks import STOP_TIMEOUT_S, cancel_and_wait
from runs import Run, RunStatus

logger = logging.getLogger(__name__)

# Longer than the client's reconnect gaps, so a reconnect is not taken for leaving.
UNWATCHED_GRACE_S = 15.0

type Send = Callable[[], Awaitable[None]]


@dataclass
class _Hold:
    payload: object
    send: Send | None
    timer: asyncio.Task[None] | None = None


class ParkNotices:
    """One held notice per parked run, sent once that run has gone unwatched for the grace."""

    def __init__(
        self,
        *,
        grace_s: float = UNWATCHED_GRACE_S,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._grace_s: float = grace_s
        self._sleep: Callable[[float], Awaitable[None]] = sleep
        self._holds: dict[str, _Hold] = {}

    def hold(self, run: Run, payload: object, send: Send) -> None:
        """Hold ``send`` until ``run`` is left while still parked on ``payload``. A later
        park of the same run replaces the hold."""
        previous = self._holds.get(run.id)
        if previous is not None:
            self._release(previous)
        hold = _Hold(payload, send)
        self._holds[run.id] = hold
        run.stream.when_unwatched(lambda: self._left(run, hold))

    async def flush(self) -> None:
        """Wait for every running grace timer to finish."""
        timers = [h.timer for h in self._holds.values() if h.timer is not None]
        if timers:
            _ = await asyncio.gather(*timers, return_exceptions=True)

    async def drain(self) -> None:
        """Cancel every pending timer and forget every hold."""
        holds = list(self._holds.values())
        self._holds.clear()
        for hold in holds:
            await cancel_and_wait(hold.timer, STOP_TIMEOUT_S)
            hold.send = None

    def _release(self, hold: _Hold) -> None:
        # A stale stream listener still names this hold; clearing it frees the parked turn.
        if hold.timer is not None:
            _ = hold.timer.cancel()
        hold.send = None
        hold.payload = None

    def _settled(self, run: Run, hold: _Hold) -> bool:
        return (
            self._holds.get(run.id) is not hold
            or run.status is not RunStatus.awaiting_input
            or run.parked_payload is not hold.payload
        )

    def _left(self, run: Run, hold: _Hold) -> None:
        if self._settled(run, hold):
            if self._holds.get(run.id) is hold:
                del self._holds[run.id]
            return
        if hold.timer is not None:
            _ = hold.timer.cancel()
        hold.timer = asyncio.get_running_loop().create_task(
            self._after_grace(run, hold), name=f"park-notice-{run.id}"
        )
        run.stream.when_unwatched(lambda: self._left(run, hold))

    async def _after_grace(self, run: Run, hold: _Hold) -> None:
        await self._sleep(self._grace_s)
        if self._settled(run, hold):
            if self._holds.get(run.id) is hold:
                del self._holds[run.id]
            return
        if run.stream.watcher_count > 0:
            return
        del self._holds[run.id]
        send, hold.send, hold.payload = hold.send, None, None
        if send is None:
            return
        try:
            await send()
        except Exception:
            logger.warning("held notice for run %s failed to send", run.id, exc_info=True)
