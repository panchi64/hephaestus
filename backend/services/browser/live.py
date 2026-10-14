"""One conversation's browser session, and the login profile every session shares.

Split from the manager because these two answer different questions. The manager decides
*which* conversations hold a browser and for how long; this decides what one session is —
how it attaches without owning the browser, how it can tell somebody is using it, and what
of it survives being torn down.

**One profile, on the host.** Cookies and local storage live in a single file in the data
directory, loaded into a conversation's browser context when it attaches and merged back
when it saves, so a login set up once serves every conversation. Only the active sessions'
copies are ever in the VM.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
from collections.abc import Callable, Hashable, Mapping, Sequence
from pathlib import Path
from typing import Protocol, final, override, runtime_checkable

from playwright.async_api import Page
from pydantic_ai_harness.playwright import PlaywrightBrowserSession

from services.shapes import as_dict, dict_list, parse_json

logger = logging.getLogger(__name__)

#: A Playwright storage state: cookies and per-origin local storage.
type StorageStateData = Mapping[str, object]


class Tab(Protocol):
    """The slice of a browser tab the session reads."""

    @property
    def url(self) -> str: ...


@runtime_checkable
class BrowserContextLike(Protocol):
    """The slice of a Playwright browser context the session reads: its open tabs and its
    cookie jar."""

    @property
    def pages(self) -> Sequence[Tab]: ...

    async def storage_state(self) -> StorageStateData: ...


class CdpSession(Protocol):
    """The slice of a Playwright CDP session the live view drives. Playwright types
    ``send`` and ``on`` too loosely (a bare ``Dict``, per-event overloads) to use as is."""

    async def send(self, method: str, params: dict[str, object] | None = None) -> object: ...

    def on(self, event: str, f: Callable[[dict[str, object]], None]) -> None: ...

    async def detach(self) -> None: ...


def context_of(page: object) -> BrowserContextLike | None:
    """The browser context a page belongs to, or None when it does not expose one."""
    candidate: object = getattr(page, "context", None)
    return candidate if isinstance(candidate, BrowserContextLike) else None


@final
class ControlledBrowserSession(PlaywrightBrowserSession):
    """A harness browser session that never closes the browser it attached to.

    The base session owns its browser handle and closes it on exit, which is correct when
    it launched one. Ours is a CDP attachment to a browser other conversations — and the
    operator — are still using, so dropping the handle before teardown leaves the pages,
    the context and the driver to be cleaned up while the browser itself keeps running.
    Playwright's CDP-attached ``close()`` happens to only close the transport today; not
    relying on that is the point — this holds whether or not that stays true.
    """

    @override
    async def __aexit__(self, exc_type: type[BaseException] | None, *args: object) -> None:
        self._browser = None
        await super().__aexit__(exc_type, *args)


class Profile:
    """The login profile every conversation's browser shares. ``path`` ``None`` keeps
    nothing (tests, mostly)."""

    def __init__(self, path: Path | None) -> None:
        self.path: Path | None = path
        self._lock: asyncio.Lock = asyncio.Lock()

    def load(self) -> dict[str, object] | None:
        """The saved profile; ``None`` when there is none or it cannot be read, since
        starting logged out is recoverable and refusing to attach is not."""
        if self.path is None or not self.path.exists():
            return None
        try:
            return as_dict(parse_json(self.path.read_text()))
        except Exception:
            logger.debug("browser: could not read the saved profile", exc_info=True)
            return None

    async def save(self, base: StorageStateData | None, current: StorageStateData) -> None:
        """Apply what one session changed since ``base`` to the profile on disk, leaving
        what other sessions saved in the meantime."""
        if self.path is None:
            return
        async with self._lock:
            merged = merge_state(base or {}, current, self.load() or {})
            write_state(self.path, merged)

    def forget(self) -> None:
        if self.path is not None:
            self.path.unlink(missing_ok=True)


def merge_state(
    base: StorageStateData, current: StorageStateData, saved: StorageStateData
) -> dict[str, object]:
    """Three-way merge of Playwright storage states: what ``current`` added, changed or
    removed relative to ``base``, applied to ``saved``."""

    def cookie_key(cookie: dict[str, object]) -> Hashable:
        return (cookie.get("name", ""), cookie.get("domain", ""), cookie.get("path", ""))

    def origin_key(origin: dict[str, object]) -> Hashable:
        return origin.get("origin", "")

    def merged(field: str, key: Callable[[dict[str, object]], Hashable]) -> list[dict[str, object]]:
        before = {key(item): item for item in dict_list(base.get(field))}
        after = {key(item): item for item in dict_list(current.get(field))}
        result = {key(item): item for item in dict_list(saved.get(field))}
        for gone in before.keys() - after.keys():
            _ = result.pop(gone, None)
        for name, item in after.items():
            if before.get(name) != item:
                result[name] = item
        return list(result.values())

    return {"cookies": merged("cookies", cookie_key), "origins": merged("origins", origin_key)}


class LiveBrowser:
    """One conversation's browser: the harness session, and the profile it saves into."""

    def __init__(
        self,
        key: str,
        token: str,
        session: ControlledBrowserSession,
        profile: Profile | None = None,
        context: BrowserContextLike | None = None,
        loaded: StorageStateData | None = None,
    ) -> None:
        self.key: str = key
        self.token: str = token
        self.session: ControlledBrowserSession = session
        self.profile: Profile | None = profile
        #: The profile as this session last loaded or saved it; the base of the next merge.
        self._loaded: StorageStateData | None = loaded
        #: The page and CDP session the live view's input goes through.
        self.input_channel: tuple[Page, CdpSession] | None = None
        # The browser context the session attached to — the conversation's tabs, rather than the
        # harness's view of it. See `open_pages`.
        self.context: BrowserContextLike | None = context
        self._last_used: float = time.monotonic()
        # What the last sweep saw, so the next one can tell "nobody touched this" from
        # "the operator has been working in it" — see `moved_since_last_sweep`.
        self._events_seen: int = session.events_recorded
        self._urls_seen: tuple[str, ...] = self._open_urls()

    @property
    def last_used(self) -> float:
        """The monotonic time of the last call on this session."""
        return self._last_used

    @last_used.setter
    def last_used(self, value: float) -> None:
        self._last_used = value

    def touch(self) -> None:
        self._last_used = time.monotonic()

    def idle_seconds(self, now: float) -> float:
        return now - self._last_used

    @property
    def page_url(self) -> str:
        page = self.session.page
        return (page.url or "") if page is not None else ""

    @property
    def open_pages(self) -> list[Tab]:
        """Every tab really open in this conversation's browser context.

        Asked of the browser context rather than of ``session.pages``, which holds only the
        tabs the harness wired: the one it opened plus popups a page spawned. A tab the
        operator opened themselves is in neither, and judging the context by that list would
        call a browser they are working in empty — then close it under them, since tearing
        the session down drops the CDP client the whole context belongs to. The tracked
        list is the fallback for a session that never handed one over.
        """
        context = self.context
        if context is not None:
            try:
                return list(context.pages)
            except Exception:  # noqa: BLE001 — a context whose browser is gone answers here
                return []
        return list(self.session.pages)

    @property
    def abandoned(self) -> bool:
        """True once the context has no tabs left — the last one was closed, and
        there is nothing for this session to be *about* any more."""
        return not self.open_pages

    def moved_since_last_sweep(self) -> bool:
        """Whether anything happened here since the previous sweep asked.

        The idle clock is only touched by the agent's own tool calls, so an operator
        reading, clicking and typing in the view all afternoon would look perfectly idle
        and be reaped mid-login. The harness counts every browser event it records, and the
        address bars move on navigations it never sees — every tab's, not just the agent's,
        or working in a tab of one's own would still read as idle. Either changing is
        somebody using this browser. Stateful by necessity — it consumes what it observed,
        so only the sweep may call it.
        """
        events = self.session.events_recorded
        urls = self._open_urls()
        moved = events != self._events_seen or urls != self._urls_seen
        self._events_seen, self._urls_seen = events, urls
        return moved

    def _open_urls(self) -> tuple[str, ...]:
        return tuple(page.url or "" for page in self.open_pages)

    async def teardown(self) -> None:
        """Save into the profile, then close the session. Never raises."""
        await self.save()
        with contextlib.suppress(Exception):
            await self.session.__aexit__(None, None, None)

    async def save(self) -> None:
        """Merge this session's cookies and local storage into the profile. Best-effort:
        a browser that is already gone has nothing left to read."""
        context = self.context if self.context is not None else context_of(self.session.page)
        if self.profile is None or context is None:
            return
        try:
            state = await context.storage_state()
            await self.profile.save(self._loaded, state)
        except Exception:
            logger.debug("browser: could not save the profile from %s", self.key, exc_info=True)
            return
        self._loaded = state


def write_state(path: Path, state: StorageStateData) -> None:
    """Write a storage state atomically, owner-only.

    Atomic because a torn write is worse than no file at all: the next attach would read
    half a cookie jar and fail rather than simply starting logged out. Owner-only because
    these *are* the sessions — anything that can read the file is logged into whatever the
    operator logged into.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(state, handle)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
