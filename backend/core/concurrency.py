"""Bounded fan-out.

Several places in the backend have the same shape: a list of independent awaitables that
should run concurrently, but not *all* concurrently — a mail listing's per-message
fetches, a reaper's per-session teardowns. Unbounded
``asyncio.gather`` over a page of remote calls is a burst the far side may rate-limit;
running them one at a time turns one round trip into fifty.

The cap is the caller's to choose, because what it protects differs: an API's rate limit,
a browser's memory, a disk. This module owns only the plumbing.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Iterable


async def gather_bounded[T](awaitables: Iterable[Awaitable[T]], max_concurrency: int) -> list[T]:
    """Await everything with at most ``max_concurrency`` in flight, results in input order.

    Failure behaves like :func:`asyncio.gather` with no ``return_exceptions``: the first
    exception propagates. A caller that wants one leg's failure isolated should catch
    inside its own coroutine — which is the honest place for that decision, since only the
    caller knows whether a missing result is a degraded answer or a broken one.
    """
    semaphore = asyncio.Semaphore(max(1, max_concurrency))

    async def slot(awaitable: Awaitable[T]) -> T:
        async with semaphore:
            return await awaitable

    return list(await asyncio.gather(*(slot(a) for a in awaitables)))
