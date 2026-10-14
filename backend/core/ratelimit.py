"""A small in-memory token-bucket rate limiter — the shared throttle substrate.

One process, one operator: a flood of requests (uploads, `UP-4`) is a runaway loop
or a stuck client, not contention between users, so an in-memory token bucket keyed
by an arbitrary string is the right grain — no Redis, no cross-process coordination.
A bucket holds up to ``burst`` tokens and refills at ``rate_per_second`` tokens a
second; each allowed action spends one. When the bucket is dry the limiter reports
how long until the next token so the caller can surface a ``Retry-After``.

Deliberately generic so any endpoint that needs throttling reuses it rather than
re-implementing the bucket. The clock is injectable so behavior is testable without
sleeping.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from core.exceptions import RateLimitedError


@dataclass
class _Bucket:
    tokens: float
    updated: float


class RateLimiter:
    """A per-key token bucket. ``check(key)`` spends one token or raises."""

    def __init__(
        self,
        *,
        rate_per_second: float,
        burst: int,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._rate: float = rate_per_second
        self._burst: float = float(burst)
        self._now: Callable[[], float] = now
        self._buckets: dict[str, _Bucket] = {}

    def check(self, key: str) -> None:
        """Spend one token for ``key``; raise :class:`RateLimitedError` when dry.

        Lazily refills the bucket for the time elapsed since its last use (capped at
        ``burst``), so an idle key is always allowed and a hot one throttles to the
        configured rate."""
        now = self._now()
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = _Bucket(tokens=self._burst, updated=now)
            self._buckets[key] = bucket
        else:
            elapsed = max(0.0, now - bucket.updated)
            bucket.tokens = min(self._burst, bucket.tokens + elapsed * self._rate)
            bucket.updated = now
        if bucket.tokens < 1.0:
            deficit = 1.0 - bucket.tokens
            retry_after = deficit / self._rate if self._rate > 0 else float("inf")
            raise RateLimitedError(retry_after)
        bucket.tokens -= 1.0
