"""VMBrowser — the agent's browser is web fetch's Chromium in the platform VM.

Each conversation attaches its own browser context to it over CDP, so tabs and history
stay per conversation, while the logins they carry are one profile the session manager
keeps on the host. Page JavaScript never runs on the operator's machine, and every
request leaves through the platform proxy, which reaches public addresses only.

Web fetch owns the browser's lifecycle (start, offline suspension, restart after a VM
reset); this only waits for it and hands out where to attach.
"""

from __future__ import annotations

from typing import Any

from services.webfetch import ManagedBrowser
from services.webfetch.stealth import INIT_SCRIPT


class VMBrowser:
    def __init__(self, browser: ManagedBrowser, *, launch_timeout_s: float) -> None:
        self._browser = browser
        self._launch_timeout_s = launch_timeout_s

    @property
    def cdp_url(self) -> str | None:
        return self._browser.cdp_url

    async def ensure(self) -> str | None:
        """Where to attach, waiting for a bring-up in progress; ``None`` when the browser
        is not coming up (no VM, offline, or disabled)."""
        if self.cdp_url is None:
            await self._browser.wait_available(self._launch_timeout_s)
        return self.cdp_url

    async def apply_stealth(self, page: Any) -> None:
        """The same disguise every fetched page gets: the automation tells masked, and
        the headless shell's user agent and client hints replaced with a desktop one's."""
        await page.add_init_script(INIT_SCRIPT)
        await self._browser.apply_stealth(page)
