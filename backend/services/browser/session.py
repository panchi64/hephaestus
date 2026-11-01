"""The agent's controllable browser — one live session per conversation, in the VM.

`pydantic_ai_harness.playwright` gives the model eighteen typed browser tools; what it
does *not* give is a browser that outlives a turn. `PlaywrightBrowser.wrap_run` opens the
session when a run starts and closes it when the run ends, which is right for a one-shot
agent and wrong for a chat thread: an operator who watches the agent log into a site and
then says "now open the billing page" is talking about *that* page. So the capability's
per-run lifecycle is bypassed here and the session is keyed by conversation instead,
reaped on idleness the way a sandbox session is.

**Each conversation gets its own browser context** on the services VM's Chromium
(``VMBrowser``), so tabs and history stay per conversation. The operator watches and
drives it through the app's live view (``view.py``); nothing arbitrates between the two
drivers, deliberately.

**The logins are one profile** (``live.Profile``), loaded into every context at attach and
merged back on each sweep and at teardown, so a login set up once serves every
conversation.

**The egress guard is deliberately left off.** ``EgressPolicy`` enforces its allowlist
with Playwright routing, which turns on CDP's Fetch domain; ``services/webfetch/browser``
documents why that is unacceptable here (bot walls detect it). Passing
``block_private_addresses=False`` with no allowlist leaves the policy unenforced, so the
services VM, which reaches public addresses only (``public_cidrs()``), remains the single
enforcement point.
The default is ``True``, so this must be explicit.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import time
from pathlib import Path
from typing import Protocol, cast

from playwright.async_api import StorageState
from pydantic_ai_harness.playwright import EgressPolicy

from core.periodic import PeriodicTask

from .live import ControlledBrowserSession, LiveBrowser, Profile, context_of

logger = logging.getLogger(__name__)


class BrowserHost(Protocol):
    """The slice of the VM's browser that sessions attach to and disguise pages through."""

    @property
    def cdp_url(self) -> str | None: ...

    async def ensure(self) -> str | None: ...

    async def apply_stealth(self, page: object) -> None: ...


class BrowserSessionManager:
    """Maps a conversation to its live browser, reaping idle ones.

    Degrades rather than fails: when the VM's browser is not up (no VM, offline mode,
    web fetch disabled) :meth:`acquire` returns ``None`` and the browse tools tell the
    model so, exactly as web fetch degrades under the same conditions.
    """

    def __init__(
        self,
        host: BrowserHost,
        *,
        idle_ttl_s: float,
        reap_interval_s: float,
        max_live: int,
        state_dir: Path | None = None,
    ) -> None:
        self._host: BrowserHost = host
        self._idle_ttl: float = idle_ttl_s
        self._max_live: int = max(1, max_live)
        self.profile: Profile = Profile(
            state_dir / "profile.json" if state_dir is not None else None
        )
        self._sessions: dict[str, LiveBrowser] = {}
        self._lock: asyncio.Lock = asyncio.Lock()
        # Per-conversation creation locks. Tool calls within one model response run
        # concurrently, so two browse tools can race the first acquire of a thread; without
        # this they would each attach a session and one would be silently orphaned.
        self._creating: dict[str, asyncio.Lock] = {}
        # Conversations whose browser the *operator* ended. A record of a decision, kept so
        # the next tool call reports it instead of quietly starting a replacement. Cleared
        # the moment a session attaches again, which only something explicit can cause.
        self._closed: set[str] = set()
        self._reaper: PeriodicTask = PeriodicTask(
            "browser-reaper",
            interval_s=reap_interval_s,
            work=self.sweep,
            logger=logger,
        )

    @property
    def sessions(self) -> dict[str, LiveBrowser]:
        """The live sessions by conversation key; mutated only under this manager's lock."""
        return self._sessions

    @property
    def creating(self) -> dict[str, asyncio.Lock]:
        """The per-conversation creation locks."""
        return self._creating

    # ── lookup ───────────────────────────────────────────────────────────────────

    def existing(self, key: str) -> LiveBrowser | None:
        """This conversation's browser if it already has one, **without** starting it —
        so a turn that never browsed pays nothing."""
        return self._sessions.get(key)

    def closed(self, key: str) -> bool:
        """Whether the operator ended this conversation's browser, rather than us.

        A session reaped for idleness is ours to re-attach silently; one the operator ended
        is a thing they did, and the honest report is that they did it.
        """
        return key in self._closed

    # ── lifecycle ────────────────────────────────────────────────────────────────

    async def acquire(self, key: str, *, reopen: bool = True) -> LiveBrowser | None:
        """This conversation's browser, attaching one on first use.

        Returns ``None`` when no browser could be opened, which is a degraded capability
        rather than an error: the caller reports it to the model.

        ``reopen`` is what a caller says when it may start a browser the operator ended.
        The browse tools pass False for everything except an explicit ``navigate``
        (:data:`tools.browse.REOPENING_TOOLS`), so ending it is not silently overruled.
        """
        live = self._sessions.get(key)
        if live is not None and (self._host.cdp_url is None or live.abandoned):
            # The browser went away (a VM reset, offline mode) or every tab was closed.
            # The sweep notices on its own clock; dropping it here keeps the next call
            # from meeting raw Playwright errors.
            abandoned = live.abandoned and self._host.cdp_url is not None
            await self.release(key)
            if abandoned:
                self._closed.add(key)
            live = None
        if live is None and key in self._closed and not reopen:
            return None
        if live is None:
            lock = self._creating.setdefault(key, asyncio.Lock())
            async with lock:
                live = self._sessions.get(key)
                if live is None:
                    live = await self._attach(key)
                    if live is None:
                        return None
                    # A browser is open again, so whatever was ended before is history.
                    self._closed.discard(key)
                    async with self._lock:
                        self._sessions[key] = live
                    await self.enforce_cap()
        live.touch()
        return live

    async def _attach(self, key: str) -> LiveBrowser | None:
        cdp_url = await self._host.ensure()
        if cdp_url is None:
            return None
        loaded = self.profile.load()
        session = ControlledBrowserSession(
            # Unenforced by design — the proxy is the egress boundary. See the module
            # docstring; the default is True, so this must be said out loud.
            policy=EgressPolicy(block_private_addresses=False),
            cdp_url=cdp_url,
            storage_state=cast(StorageState | None, loaded),
        )
        _ = await session.__aenter__()
        try:
            page = await session.ensure_page()
            await self._prepare(page)
        except Exception:
            logger.warning("browser: could not open a page for %s", key, exc_info=True)
            with contextlib.suppress(Exception):
                await session.__aexit__(None, None, None)
            return None
        return LiveBrowser(
            key,
            secrets.token_urlsafe(32),
            session,
            self.profile,
            # The context itself, so the session counts tabs the harness never wired
            # (ones the operator opened). See `live.LiveBrowser.open_pages`.
            context_of(page),
            loaded,
        )

    async def _prepare(self, page: object) -> None:
        """Bring a freshly-attached page up to the same disguise every fetched page gets.

        Best-effort: a page that browses without the automation masking is worse at passing
        for a real browser, not broken.
        """
        with contextlib.suppress(Exception):
            await self._host.apply_stealth(page)

    async def release(self, key: str) -> None:
        """Tear down one conversation's browser. Idempotent."""
        async with self._lock:
            live = self._sessions.pop(key, None)
        if live is not None:
            await live.teardown()

    async def close(self, key: str) -> None:
        """The operator ends this conversation's browser. Its logins stay in the profile."""
        await self.release(key)
        self._closed.add(key)

    async def purge(self, key: str) -> None:
        """Forget a deleted conversation's browser. The profile is shared, so it stays.
        Idempotent."""
        await self.release(key)
        self._closed.discard(key)

    async def forget_profile(self) -> None:
        """Log out of everything: end every session without saving, and delete the
        profile."""
        async with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for live in sessions:
            live.profile = None
            await live.teardown()
        with contextlib.suppress(OSError):
            self.profile.forget()

    async def enforce_cap(self) -> None:
        """Evict least-recently-used sessions past the cap.

        Each live session holds a Playwright driver subprocess and a browser context in the
        VM, so this is a real resource ceiling rather than tidiness.
        """
        async with self._lock:
            if len(self._sessions) <= self._max_live:
                return
            now = time.monotonic()
            ordered = sorted(
                self._sessions.values(),
                key=lambda live: live.idle_seconds(now),
                reverse=True,
            )
            evicted = ordered[: len(self._sessions) - self._max_live]
            for live in evicted:
                _ = self._sessions.pop(live.key, None)
        for live in evicted:
            logger.info("browser: evicted the session for %s (over the live cap)", live.key)
            await live.teardown()

    def _prune_creation_locks(self) -> None:
        """Drop creation locks for conversations that hold no session and have nobody
        mid-acquire. Every other map here is bounded or reaped; without this one pass the
        locks would be the one structure that only ever grew, an entry per conversation
        that ever browsed, for the life of the process.

        Safe against a concurrent :meth:`acquire`: `setdefault` and an uncontended
        `Lock.acquire()` both complete without suspending, so no caller can be observed
        between choosing its lock and holding it. A key dropped while unlocked simply gets
        a fresh lock on the next acquire.
        """
        self._creating = {
            key: lock
            for key, lock in self._creating.items()
            if lock.locked() or key in self._sessions
        }

    async def start(self) -> None:
        await self._reaper.start()

    async def stop(self) -> None:
        """Put the reaper down, then tear every live session down.

        The order matters and the loop helper deliberately owns only the first half: a
        sweep still running while sessions are being torn down would be two things
        closing the same browser.
        """
        await self._reaper.stop()
        async with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for live in sessions:
            await live.teardown()

    async def sweep(self) -> None:
        """Reap what nobody is using, and save what somebody is.

        A session ends here when the browser went away (a VM reset, offline mode), when
        every tab was closed, or when it sat unused — counting the operator's own clicking
        in the view, not only the agent's tool calls. Only closed tabs leave a tombstone
        (:meth:`closed`): the other two are ours, and the next tool call re-attaches with
        the profile restored. A session that moved saves into the profile, so a login made
        here reaches other conversations without waiting for this one to end.
        """
        gone = self._host.cdp_url is None
        now = time.monotonic()
        reasons: list[tuple[LiveBrowser, str]] = []
        moved: list[LiveBrowser] = []
        async with self._lock:
            for live in list(self._sessions.values()):
                if gone:
                    reasons.append((live, "browser unavailable"))
                elif live.abandoned:
                    reasons.append((live, "closed"))
                elif live.moved_since_last_sweep():
                    live.touch()
                    moved.append(live)
                elif live.idle_seconds(now) >= self._idle_ttl:
                    reasons.append((live, "idle"))
            for live, reason in reasons:
                _ = self._sessions.pop(live.key, None)
                if reason == "closed":
                    self._closed.add(live.key)
            self._prune_creation_locks()
        for live, reason in reasons:
            logger.info("browser: reaped the session for %s (%s)", live.key, reason)
            await live.teardown()
        for live in moved:
            await live.save()
