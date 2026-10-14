"""Per-domain politeness — space out requests to one site.

A burst of fetches at one host trips its rate limiter (the "whoa there, slow down" class of
block, distinct from fingerprinting), and hammering a site in parallel reads as bot-like.
This serializes same-host fetches with a minimum gap between them, while letting different
hosts run concurrently (bounded separately by the browser's semaphore). Keyed by hostname —
no public-suffix list, and subdomains throttle independently, which is the safe default.

In-memory and per-process, like the cookie jar. ``min_interval_s <= 0`` makes ``slot`` a
no-op, so the feature is a single config flip.
"""

from __future__ import annotations

import asyncio
import time
import urllib.parse
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import ClassVar, final

_MAX_TRACKED = 4096  # bound the per-host bookkeeping in a long-running process


@final
class _Slot:
    """One host's politeness state: its serialization lock and the last-finish time."""

    __slots__: ClassVar[tuple[str, ...]] = ("lock", "last")

    def __init__(self) -> None:
        self.lock: asyncio.Lock = asyncio.Lock()
        self.last: float = 0.0


class DomainThrottle:
    """Enforce a minimum interval between fetches to the same host.

    Holding the per-host lock across the request both spaces requests out and prevents
    same-host fetches from running in parallel; other hosts are unaffected."""

    def __init__(self, *, min_interval_s: float) -> None:
        self._min_interval_s: float = min_interval_s
        self._slots: dict[str, _Slot] = {}  # host -> its lock + last-finish time

    def tracks(self, host: str) -> bool:
        """Whether ``host`` still has a slot in the pruned table."""
        return host in self._slots

    @asynccontextmanager
    async def slot(self, url: str) -> AsyncGenerator[None]:
        """Acquire this host's politeness slot: serialize with other same-host fetches and
        wait out any remaining gap since the last one finished, then run."""
        if self._min_interval_s <= 0:
            yield
            return
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
        if not host:
            yield
            return
        slot = self._slots.get(host)
        if slot is None:
            if len(self._slots) >= _MAX_TRACKED:
                # Prune only idle hosts — never drop a slot whose lock is held, or two
                # same-host fetches would acquire different locks and run in parallel.
                for h in [h for h, s in self._slots.items() if not s.lock.locked()]:
                    del self._slots[h]
            slot = self._slots.setdefault(host, _Slot())
        async with slot.lock:
            wait = self._min_interval_s - (time.monotonic() - slot.last)
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                yield
            finally:
                slot.last = time.monotonic()
