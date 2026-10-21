"""Noticing that the platform VM died while it was up, and restarting it once.

A VM that stops answering leaves every command waiting on a guest that is gone. Two missed
probes in a row count as dead, because one probe can time out on a busy guest. The first
death is answered with a reset; a second within :data:`REPEAT_WINDOW_S` leaves the VM down
and tells the operator, because a VM that keeps dying needs a person, not another reboot.
Bringing a VM that was left down back up reopens the automatic reset.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable

from core.periodic import PeriodicTask

from .platform import PlatformVM

logger = logging.getLogger(__name__)

PROBE_INTERVAL_S = 15.0
MISSES_TO_DIE = 2
REPEAT_WINDOW_S = 600.0

type Notify = Callable[[str, str], Awaitable[object]]


class VMLiveness:
    """Probes a running :class:`PlatformVM` and answers its death. ``notify`` takes a title
    and a body for the operator."""

    def __init__(
        self,
        vm: PlatformVM,
        notify: Notify,
        *,
        interval_s: float = PROBE_INTERVAL_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._vm: PlatformVM = vm
        self._notify: Notify = notify
        self._clock: Callable[[], float] = clock
        self._misses: int = 0
        self._last_death: float | None = None
        self._held: bool = False
        self._probe_task: PeriodicTask = PeriodicTask(
            "vm-liveness", interval_s=interval_s, work=self.probe, logger=logger
        )

    async def start(self) -> None:
        await self._probe_task.start()

    async def stop(self) -> None:
        await self._probe_task.stop()

    async def probe(self) -> None:
        """One probe. Does nothing while the VM is not up: a boot in progress, or a VM
        already marked down, is not a death to answer."""
        if not self._vm.available:
            self._misses = 0
            return
        if await self._vm.responds():
            self._misses = 0
            if self._held:
                # The operator brought a VM we left down back up: it earns its automatic
                # reset again.
                self._held = False
                self._last_death = None
            return
        self._misses += 1
        if self._misses < MISSES_TO_DIE:
            return
        self._misses = 0
        await self._died()

    async def _died(self) -> None:
        now = self._clock()
        repeated = self._last_death is not None and now - self._last_death < REPEAT_WINDOW_S
        self._last_death = now
        self._vm.mark_unavailable("the platform VM stopped answering")
        if repeated:
            self._held = True
            logger.warning("platform VM: died again within the window; leaving it down")
            _ = await self._notify(
                "The platform VM keeps stopping",
                "It stopped answering twice within ten minutes, so it was left down. "
                + "Reset it from Settings once you have looked at why.",
            )
            return
        logger.warning("platform VM: stopped answering; restarting it")
        _ = await self._notify(
            "The platform VM stopped",
            "It stopped answering and is being restarted. Changes made in it since the "
            + "last command may be lost.",
        )
        await self._vm.reset()
