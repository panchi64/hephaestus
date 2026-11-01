"""The run-terminal dispatch point — features compose the policy, the substrate
stays feature-free.

``runs/`` only knows it holds an optional ``on_terminal`` callback; this dispatcher
is what the app injects there. Features contribute hooks through their manifests
instead of the lifespan hardcoding every feature's terminal behavior in one
closure:

- a **sync hook** runs inline in the terminal transition, for bookkeeping that must
  be observable before anything else reacts (resolving a waiter future another
  request is awaiting);
- an **async hook** runs as a background task — tracked in ``tasks`` so shutdown
  can drain them before the stores they read stop — and receives ``watched``,
  whether anyone was subscribed to the run's stream at the moment it settled
  (captured synchronously here, because reading it later would race the stream's
  own subscriber cleanup).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from core.tasks import STOP_TIMEOUT_S

if TYPE_CHECKING:
    from runs import Run

logger = logging.getLogger(__name__)

type SyncRunTerminalHook = Callable[["Run"], None]
type RunTerminalHook = Callable[["Run", bool], Awaitable[None]]


class RunTerminalDispatcher:
    """Fans one terminal transition out to every registered hook, each isolated —
    one feature's failure never suppresses another's."""

    def __init__(self) -> None:
        # The shared bucket of in-flight terminal background tasks (also reachable
        # as ``app.state.run_terminal_tasks``) — drained at shutdown, and awaitable
        # by tests as "every pending terminal reaction has settled".
        self.tasks: set[asyncio.Task[None]] = set()
        self._sync: list[SyncRunTerminalHook] = []
        self._async: list[RunTerminalHook] = []

    def add_sync(self, hook: SyncRunTerminalHook) -> None:
        self._sync.append(hook)

    def add(self, hook: RunTerminalHook) -> None:
        self._async.append(hook)

    def __call__(self, run: Run) -> None:
        for hook in self._sync:
            try:
                hook(run)
            except Exception:
                logger.exception("run-terminal: sync hook failed for run %s", run.id)
        watched = run.stream.watcher_count > 0
        for hook in self._async:
            task = asyncio.create_task(self._invoke(hook, run, watched))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)

    async def _invoke(self, hook: RunTerminalHook, run: Run, watched: bool) -> None:
        try:
            await hook(run, watched)
        except Exception:
            logger.exception("run-terminal: hook failed for run %s", run.id)

    async def drain(self) -> None:
        """Cancel and await every in-flight terminal task — a run finishing right at
        shutdown must not leave a task reading a closed store, and must not warn as
        destroyed-while-pending."""
        pending = list(self.tasks)
        for task in pending:
            _ = task.cancel()
        if pending:
            # Bounded: a hook that swallows the cancel must not hold the shutdown.
            _ = await asyncio.wait(pending, timeout=STOP_TIMEOUT_S)
