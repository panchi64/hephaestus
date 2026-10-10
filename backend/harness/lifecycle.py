"""One owner for the app's background start/stop order.

Everything long-lived the app brings up — write-behind drainers, sync loops,
container monitors, fire-and-forget startup tasks, clients that need closing —
registers here at its construction point, and shutdown becomes a single call
that unwinds in **reverse registration order**. Before this existed, the
lifespan's ``finally`` block was a hand-maintained mirror of the construction
sequence that had to be edited in exactly the right position every time a
capability landed; now the teardown position is implied by the setup position,
and only genuinely order-sensitive stops need thought (register later to stop
earlier).

``start`` runs the unit's startup *immediately* — registration does not defer
or reorder bring-up, so a capability that later construction depends on is live
by the next line, exactly as when the lifespan called ``.start()`` inline.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass

from core.tasks import STOP_TIMEOUT_S, cancel_and_wait

logger = logging.getLogger(__name__)

# A stop callable may be sync (e.g. cancelling in-flight work) or async (draining it).
type StopFn = Callable[[], Awaitable[None] | None]


@dataclass(frozen=True)
class _Unit:
    name: str
    stop: StopFn


class LifecycleRegistry:
    """Ordered start/stop for everything long-lived the app owns.

    Stops run in reverse registration order, each isolated — one unit failing to
    stop is logged and never prevents the rest from stopping.
    """

    def __init__(self) -> None:
        self._units: list[_Unit] = []

    async def start(
        self,
        name: str,
        *,
        start: Callable[[], Awaitable[object]],
        stop: StopFn,
    ) -> None:
        """Run ``start`` now; record ``stop`` for reverse-order shutdown.

        Startup failures propagate — a capability that can't come up should fail
        the boot loudly, not limp into a half-wired process.
        """
        await start()
        self._units.append(_Unit(name, stop))

    def on_stop(self, name: str, stop: StopFn) -> None:
        """Record a stop for something started elsewhere (or needing no start) —
        a client to close, a supervisor whose children another service brings up."""
        self._units.append(_Unit(name, stop))

    def track(self, name: str, coro: Coroutine[object, object, None]) -> asyncio.Task[None]:
        """Run a fire-and-forget background coroutine, cancelled (and awaited, so it
        never warns as destroyed-while-pending) at shutdown if still running."""
        task = asyncio.create_task(coro, name=name)

        async def _cancel() -> None:
            await cancel_and_wait(task, STOP_TIMEOUT_S)

        self._units.append(_Unit(name, _cancel))
        return task

    async def stop_all(self) -> None:
        """Stop every registered unit, last-registered first. Idempotent — a second
        call finds nothing left to stop."""
        units, self._units = self._units, []
        for unit in reversed(units):
            try:
                result = unit.stop()
                if inspect.isawaitable(result):
                    await result
            except Exception:
                logger.exception("lifecycle: stopping %s failed", unit.name)
