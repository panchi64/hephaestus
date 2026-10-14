"""Stopping a background task without trusting it to stop.

``task.cancel()`` followed by ``await task`` was written out by hand in a dozen places, and
every copy shared one assumption: that a cancelled task gets out of the way promptly. A task
that catches ``CancelledError`` and carries on — or sits in a cleanup that itself blocks —
turns that ``await`` into a shutdown that never ends. :func:`cancel_and_wait` is the one
spelling, and it bounds the wait.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

logger = logging.getLogger(__name__)

# The default patience for a task whose cancellation is expected to be instant — a loop
# parked on a sleep or a queue. Anything with real cleanup in its ``finally`` passes more.
STOP_TIMEOUT_S = 2.0


async def cancel_and_wait(task: asyncio.Task[object] | None, timeout: float) -> None:
    """Cancel ``task`` and wait up to ``timeout`` seconds for it to unwind.

    Returns once the task is done *or* the wait runs out — never raises for the task's own
    cancellation or a timeout, so a teardown loop can call it on every unit and carry on.
    A task that ignored the cancel is logged and abandoned (it is still cancelled; we just
    stop waiting). ``None`` and an already-finished task are no-ops. An exception the task
    itself ended with is not re-raised: a task being torn down has no caller left to hear it.
    """
    if task is None or task.done():
        return
    _ = task.cancel()
    # ``wait`` rather than ``wait_for``: it never cancels the task a second time and never
    # raises the task's result, which is exactly the contract above.
    done, _ = await asyncio.wait({task}, timeout=timeout)
    if not done:
        logger.warning(
            "task %s did not stop within %.1fs of being cancelled", task.get_name(), timeout
        )
        return
    with contextlib.suppress(asyncio.CancelledError, Exception):
        _ = task.exception()  # retrieve, so an error here never warns as never-retrieved
