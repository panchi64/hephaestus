"""What stands between a window's call and its handler: the rate limit, the calls waiting
on the operator, and the calls the operator said to always allow.

A pending call keeps its own parameters, so the operator confirms exactly what was asked;
it is good once, for the window that asked, for a few minutes. "Always allow" is kept per
plugin with the digest of the version it was given to, so a rewrite of the plugin's code
starts asking again.
"""

from __future__ import annotations

import json
import secrets
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from pydantic import JsonValue

from core.ratelimit import RateLimiter
from core.serde import parse_json
from services.settings_store import SettingsStore

from ..store import bridge_grants_key
from ..windows import WindowSession
from .base import Confirmation

PENDING_TTL_S = 300.0
MAX_PENDING_PER_WINDOW = 16


@dataclass(frozen=True)
class PendingCall:
    bridge_token: str
    method: str
    params: Mapping[str, JsonValue]
    confirmation: Confirmation
    expires_at: float


class BridgeGate:
    def __init__(
        self, settings: SettingsStore, *, now: Callable[[], float] = time.monotonic
    ) -> None:
        self._settings: SettingsStore = settings
        self._now: Callable[[], float] = now
        self._limiter: RateLimiter = RateLimiter(rate_per_second=10, burst=30, now=now)
        self._pending: dict[str, PendingCall] = {}

    def spend(self, window: WindowSession, method: str) -> None:
        """Raises ``RateLimitedError`` when the plugin's windows call ``method`` too fast.

        Keyed by plugin rather than window, so reopening windows neither resets the limit
        nor grows the bucket table."""
        self._limiter.check(f"{window.plugin_id}:{method}")

    def ask(
        self,
        window: WindowSession,
        method: str,
        params: Mapping[str, JsonValue],
        confirmation: Confirmation,
    ) -> str:
        """Park the call for the operator; returns the id their answer names."""
        now = self._now()
        self._pending = {k: v for k, v in self._pending.items() if v.expires_at > now}
        mine = [k for k, v in self._pending.items() if v.bridge_token == window.bridge_token]
        for stale in mine[: max(0, len(mine) - MAX_PENDING_PER_WINDOW + 1)]:
            del self._pending[stale]
        confirmation_id = secrets.token_urlsafe(16)
        self._pending[confirmation_id] = PendingCall(
            bridge_token=window.bridge_token,
            method=method,
            params=dict(params),
            confirmation=confirmation,
            expires_at=now + PENDING_TTL_S,
        )
        return confirmation_id

    def take(self, window: WindowSession, confirmation_id: str) -> PendingCall | None:
        """The parked call, once, if it is this window's and still fresh."""
        pending = self._pending.get(confirmation_id)
        if pending is None or pending.bridge_token != window.bridge_token:
            return None
        del self._pending[confirmation_id]
        return pending if pending.expires_at > self._now() else None

    async def granted(self, window: WindowSession, grant: str) -> bool:
        return grant in await self._grants(window)

    async def remember(self, window: WindowSession, grant: str) -> None:
        grants = await self._grants(window) | {grant}
        value = json.dumps({"digest": window.digest, "grants": sorted(grants)})
        await self._settings.set(window.owner_id, _key(window), value)

    async def _grants(self, window: WindowSession) -> frozenset[str]:
        raw = await self._settings.get(window.owner_id, _key(window))
        if raw is None:
            return frozenset()
        try:
            stored = parse_json(raw)
        except ValueError:
            return frozenset()
        if not isinstance(stored, dict) or stored.get("digest") != window.digest:
            return frozenset()
        grants = stored.get("grants")
        if not isinstance(grants, list):
            return frozenset()
        return frozenset(g for g in grants if isinstance(g, str))


def _key(window: WindowSession) -> str:
    return bridge_grants_key(window.plugin_id)
