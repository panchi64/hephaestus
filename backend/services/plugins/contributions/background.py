"""``background``: a built-in plugin's long-running work (a sync loop, a gateway socket),
running only while the plugin is switched on."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import override

from .base import BUILTIN_ONLY, Described, TypedContribution
from .registry import CONTRIBUTIONS

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Background:
    name: str
    start: Callable[[], Awaitable[None]]
    stop: Callable[[], Awaitable[None]]


class BackgroundWork(TypedContribution[Background]):
    @override
    async def start(self, plugin: str, items: tuple[Background, ...]) -> None:
        for item in items:
            await item.start()

    @override
    async def stop(self, plugin: str, items: tuple[Background, ...]) -> None:
        # Reverse order, and every one attempted: a failed stop must not strand the rest.
        for item in reversed(items):
            try:
                await item.stop()
            except Exception:
                logger.exception("plugins: stopping %s's %s failed", plugin, item.name)

    @override
    def describe_one(self, item: Background) -> Described:
        return {"name": item.name}


BACKGROUND = CONTRIBUTIONS.register(
    BackgroundWork("background", "Background work", Background, tiers=BUILTIN_ONLY)
)
