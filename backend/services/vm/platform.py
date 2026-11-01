"""Whether machines can boot here: the host can run them and the guest image is built."""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress

import smol

from .errors import ImageBuildError, MachineUnavailableError
from .image import GuestImage

logger = logging.getLogger(__name__)


class Platform:
    """The guest image and whether this host can boot machines from it.

    Bring-up runs in the background and never blocks app start; until it finishes, or if
    it fails, :attr:`available` is False and what needs a machine says so.
    """

    def __init__(self, image: GuestImage, *, enabled: bool = True) -> None:
        self.image: GuestImage = image
        self._enabled: bool = enabled
        self._ready: asyncio.Event = asyncio.Event()
        self._failure: str | None = None
        self._task: asyncio.Task[None] | None = None

    @property
    def available(self) -> bool:
        return self._ready.is_set()

    @property
    def failure(self) -> str | None:
        return self._failure

    @property
    def progress(self) -> str | None:
        """What a bring-up in progress is doing, worded for the operator."""
        if self.available or self._failure is not None:
            return None
        if self.image.building:
            step = self.image.step
            return "Building the VM image (first start after an update; a few minutes)" + (
                f": {step}" if step else ""
            )
        return "Starting"

    async def start(self) -> None:
        if not self._enabled:
            self._failure = "the VM is disabled in settings"
            return
        self._task = asyncio.create_task(self._bring_up(), name="vm-image")

    async def stop(self) -> None:
        if self._task is not None:
            _ = self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def wait_ready(self, timeout_s: float) -> bool:
        """Wait for the image, returning early once a bring-up has ended without it."""
        if self._ready.is_set():
            return True
        task = self._task
        if task is None or task.done():
            return False
        ready = asyncio.ensure_future(self._ready.wait())
        try:
            _ = await asyncio.wait({ready, task}, timeout=timeout_s, return_when="FIRST_COMPLETED")
        finally:
            _ = ready.cancel()
        return self._ready.is_set()

    def require(self) -> str:
        """The image's path, or raise why no machine can boot."""
        if not self.available:
            raise MachineUnavailableError(self._failure or "the VM image is still being built")
        return str(self.image.path)

    async def _bring_up(self) -> None:
        self._failure = None
        ok, code, reason = await asyncio.to_thread(smol.local_availability)
        if not ok:
            self._failure = f"this machine cannot run VMs ({code}): {reason}"
            logger.warning("vm: %s", self._failure)
            return
        try:
            _ = await self.image.ensure()
        except (ImageBuildError, smol.SmolError, OSError) as exc:
            self._failure = str(exc)
            logger.warning("vm: unavailable: %s", exc)
            return
        self._ready.set()
        logger.info("vm: image ready")
