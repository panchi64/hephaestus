"""One recurring background job — the loop every reaper and sweeper in the app was
writing for itself.

Three of them existed before this file and they were the same fifteen lines three times:
create a task in ``start``, cancel-suppress-await it in ``stop``, and loop over
``sleep(interval)`` → ``try: work() except Exception: log``. Nothing was wrong with any
copy; what was wrong is that the rule holding them together — **a bad sweep must never kill
the loop** — was a comment repeated per copy rather than a property of one thing, so a
fourth copy could quietly omit it and nobody would be looking at the other three.

What it owns is deliberately only the task. A caller's own teardown stays the caller's: the
browser manager tears down every live session after its loop stops, the sandbox manager
drains its in-flight teardowns and stops its sessions, and neither of those belongs to a
timer. :meth:`stop` puts the loop down and returns.

**The interval is slept first, never last.** Every user of this wants "every N seconds from
now", not "at boot and then every N seconds": a reaper that sweeps on start does its work
against a process with nothing in it yet, and a summariser that did would run the moment the
app comes up against every thread at once. ``first_delay_s`` overrides the first sleep for
the caller that genuinely wants a different lead-in.

**The task scheduler is deliberately not on this** (:mod:`services.scheduler`). It looks
like a fourth copy and is not one: it sleeps to the *earliest due task*, recomputed after
every tick and interruptible by a wake, so its period is a property of the data rather than
a constant. Folding a due-time queue into a fixed-interval timer would mean either polling
far more often than it needs to or firing tasks late, which is two algorithms forced into
one shape for the sake of sharing a ``try/except``. Conversation titling is not one either,
for the opposite reason: it runs once, inside the turn it belongs to, with no loop at all.

It lives here and not beside :class:`harness.LifecycleRegistry`, which is where every one
of its users registers it, because the pairing is the *caller's* and not this module's: a
service imports the timer, while the registry is held one layer up by whoever assembles the
app. Putting the primitive in ``harness`` would have meant services importing the assembly
layer — the one direction ``harness/__init__`` says nothing may go — to gain a class that
imports nothing above ``logging``. Its neighbours here are the right ones:
:mod:`core.worker` is the same idea for a drained queue, and :mod:`core.concurrency` for a
bounded fan-out.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable

_logger = logging.getLogger(__name__)


class PeriodicTask:
    """A named coroutine run on a fixed interval until stopped.

    ``work`` is awaited once per period and may raise: the failure is logged at
    ``log_level`` against ``logger`` and the loop carries on, because the alternative — a
    loop that exits on its first bad sweep — is a background job that silently stops
    existing in a process that is otherwise healthy. The level is the caller's because the
    two kinds of failure are not the same news: a reaper that could not reach the
    VM this minute will try again in the next one and is ``debug``; a job
    whose whole purpose was to produce something has lost that output and is ``warning``.
    """

    def __init__(
        self,
        name: str,
        *,
        interval_s: float,
        work: Callable[[], Awaitable[object]],
        logger: logging.Logger | None = None,
        log_level: int = logging.DEBUG,
        first_delay_s: float | None = None,
    ) -> None:
        self._name = name
        self._interval_s = interval_s
        self._work = work
        self._logger = logger or _logger
        self._log_level = log_level
        self._first_delay_s = interval_s if first_delay_s is None else first_delay_s
        self._task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        """Whether the loop is currently up — what a caller's own ``start``/``stop`` test
        asserts, and the reason the task itself stays private."""
        return self._task is not None

    async def start(self) -> None:
        """Launch the loop. Idempotent: a second call while already running is a no-op
        rather than a second task nobody holds a handle to."""
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._loop(), name=self._name)

    async def stop(self) -> None:
        """Cancel the loop and wait for it to finish unwinding.

        Awaited rather than merely cancelled so a ``work`` call caught mid-await has run its
        own cleanup before the caller's teardown starts on the same state. Idempotent, and
        it clears the handle first so a stop racing a stop cancels one task, not one twice.
        """
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _loop(self) -> None:
        delay = self._first_delay_s
        while True:
            await asyncio.sleep(delay)
            delay = self._interval_s
            try:
                await self._work()
            except asyncio.CancelledError:
                # A stop landing inside the work: the loop is over, and swallowing this
                # below would turn a shutdown into a job that keeps running.
                raise
            except Exception:  # noqa: BLE001 — the loop must survive a bad run
                self._logger.log(self._log_level, "%s: run failed", self._name, exc_info=True)
