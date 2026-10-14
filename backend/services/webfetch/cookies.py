"""In-memory, per-domain cookie jar — lets a solved JS/bot challenge carry forward.

Each fetch otherwise gets a fresh context with no cookies (deliberate isolation), so a
site that gates content behind a JS challenge (Reddit's ``js_challenge``, Cloudflare's
``cf_clearance``) re-issues and re-solves it on *every* fetch — slow, and repeated
challenges from one IP read as bot-like. This caches the cookies a context accumulates and
re-seeds the ones whose domain matches the next fetch's URL, so the clearance token
persists across fetches to the same site.

Deliberately **in-memory only** (never written to disk, never sealed — there is nothing to
leak at rest) and **bounded** (a TTL plus a hard cap on entries, oldest evicted first). It
relaxes the no-shared-cookies isolation only within a single process lifetime — the
tradeoff for not re-solving every challenge. Matching uses each cookie's own ``domain``
attribute (the site set it), so no public-suffix list is needed.
"""

from __future__ import annotations

import time
import urllib.parse
from collections.abc import Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Playwright exports this shape only from its private package, so it is never imported
    # at runtime.
    from playwright._impl._api_structures import SetCookieParam
    from playwright.async_api import Cookie


def _host_matches(host: str, cookie_domain: str) -> bool:
    """Whether ``host`` is covered by a cookie scoped to ``cookie_domain`` — exact match or
    a subdomain (``www.reddit.com`` is covered by a ``.reddit.com`` cookie)."""
    d = cookie_domain.lstrip(".").lower()
    return bool(d) and (host == d or host.endswith("." + d))


def _key(cookie: SetCookieParam) -> tuple[str | None, str | None, str | None]:
    """The identity a later set overwrites: name within a (domain, path) scope."""
    return (cookie.get("name"), cookie.get("domain"), cookie.get("path"))


class DomainCookieJar:
    """A bounded, TTL'd cache of Playwright cookie dicts, shared across fetches.

    ``ttl_s`` is how long a stored cookie survives in the cache regardless of its own
    expiry (a session cookie has none); ``max_entries`` caps total size. Both bound a
    long-running process — nothing here is persisted."""

    def __init__(self, *, ttl_s: float, max_entries: int) -> None:
        self._ttl_s: float = ttl_s
        self._max: int = max(1, max_entries)
        # _key -> (cookie_dict, stored_monotonic). Insertion order = eviction order.
        self._entries: dict[
            tuple[str | None, str | None, str | None], tuple[SetCookieParam, float]
        ] = {}

    def seed_for(self, url: str) -> list[SetCookieParam]:
        """Cookies whose domain covers ``url``'s host — ready to hand to
        ``context.add_cookies``. ``_evict`` (run first) is the single owner of freshness, so
        what remains to do here is the host filter."""
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
        if not host:
            return []
        self._evict()
        return [
            cookie
            for cookie, _ in self._entries.values()
            if _host_matches(host, str(cookie.get("domain", "")))
        ]

    def store(self, cookies: Sequence[Cookie]) -> None:
        """Merge a context's cookies into the cache (a later set overwrites an earlier one
        with the same name/domain/path). Expiry is ``_evict``'s job — storing an
        already-expired cookie (the standard way a site *deletes* one) correctly clears any
        cached entry it shadows, then `_evict` drops it on the spot."""
        now_mono = time.monotonic()
        for raw in cookies:
            cookie: SetCookieParam = {**raw}
            key = _key(cookie)
            _ = self._entries.pop(key, None)  # re-insert so eviction order tracks recency
            self._entries[key] = (cookie, now_mono)
        self._evict()

    def _evict(self) -> None:
        """Drop entries past the cache TTL or their own expiry, then trim oldest to the cap."""
        now_wall = time.time()
        now_mono = time.monotonic()
        for key, (cookie, stored) in list(self._entries.items()):
            expires = cookie.get("expires", -1)
            expired = isinstance(expires, (int, float)) and 0 < expires <= now_wall
            if expired or now_mono - stored > self._ttl_s:
                del self._entries[key]
        while len(self._entries) > self._max:
            _ = self._entries.pop(next(iter(self._entries)))
