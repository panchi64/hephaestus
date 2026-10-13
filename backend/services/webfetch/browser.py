"""ManagedBrowser — the headless Chromium the web fetcher renders pages in, run in the
platform VM.

Rendering a page means executing its untrusted JavaScript, so the browser must not run
in the backend process or on the host. It runs as a service in the platform VM, as its
own unprivileged user, and we drive it over the Chrome DevTools Protocol via Playwright
``connect_over_cdp`` on a forwarded loopback port.

**SSRF is enforced at the VM's egress gateway, not by in-browser interception.** The
browser is pointed at the guest relay with ``--proxy-server``, and every request it makes
ends at the host-side gateway, which resolves each destination, pins a public IP, and
refuses non-public ones (:mod:`services.vm.egress`). Enforcing in the browser via
Playwright ``context.route`` would enable CDP's Fetch domain, which bot walls detect and
hard-block — the proxy keeps the policy while looking like an ordinary browser. The VM
has no other route out, so nothing the page does steps around it.

Bring-up is **best-effort and non-fatal** (mirrors :class:`services.searxng.ManagedSearxng`):
a VM that never comes up or a browser that never answers leaves :attr:`available` False
and web fetch degrades — it never blocks app startup. Each fetch gets a fresh, stealthed
context; total concurrency is bounded.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from playwright.async_api import Browser, BrowserContext, Playwright, async_playwright

from core.tasks import STOP_TIMEOUT_S, cancel_and_wait
from services.vm import PlatformVM, VMError

from .cdp import discover_cdp_ws
from .cookies import DomainCookieJar
from .stealth import (
    INIT_SCRIPT,
    LAUNCH_FLAGS,
    context_options,
    realistic_user_agent,
    user_agent_override,
)

logger = logging.getLogger(__name__)

_SERVICE = "chromium"
_FORWARDER = "chromium-cdp"
_USER = "browser"
_BINARY = "/opt/hephaestus/bin/headless_shell"
#: Chromium serves CDP on the guest's loopback only; the forwarder exposes it here.
_CDP_PORT = 9222
_CDP_EXPOSED = 9223


class ManagedBrowser:
    """Owns the lifecycle of the headless Chromium that runs in the platform VM.

    :attr:`available` is False until the VM is up, the browser is running and the CDP
    connection is live (or forever, if the VM never comes up or is disabled); callers check
    it and degrade. ``user_agent`` empty ⇒ derive a realistic one from the engine
    version."""

    def __init__(
        self,
        *,
        enabled: bool,
        vm: PlatformVM | None,
        startup_timeout_s: float,
        concurrency: int,
        user_agent: str,
        locale: str,
        timezone_id: str,
        cookie_ttl_s: float = 0.0,
        cookie_max: int = 2000,
    ) -> None:
        self._enabled = enabled
        self._vm = vm
        self._startup_timeout_s = startup_timeout_s
        self._sem = asyncio.Semaphore(max(1, concurrency))
        self._user_agent_override = user_agent
        self._locale = locale
        self._timezone_id = timezone_id
        # Shared across fetches so a solved challenge's clearance cookie carries forward;
        # None disables (fresh, cookieless context every fetch — the prior behaviour).
        self._jar = (
            DomainCookieJar(ttl_s=cookie_ttl_s, max_entries=cookie_max)
            if (cookie_ttl_s > 0)
            else None
        )
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self._user_agent = ""  # resolved from the override or the engine version in _bring_up
        self._browser_version = ""  # the engine version, for matching the client-hint brands
        self._ws_url: str | None = None
        self._task: asyncio.Task | None = None
        if vm is not None:
            vm.on_ready(self._after_boot)

    @property
    def available(self) -> bool:
        return self._browser is not None and self._browser.is_connected()

    @property
    def cdp_url(self) -> str | None:
        """Where another client attaches to this browser, while it is up."""
        return self._ws_url if self.available else None

    async def wait_available(self, timeout_s: float) -> bool:
        """Wait up to ``timeout_s`` for a bring-up in progress to finish."""
        task = self._task
        if task is not None and not self.available:
            await asyncio.wait({task}, timeout=timeout_s)
        return self.available

    async def start(self) -> None:
        """Begin bring-up. Returns immediately — the pull/launch/connect runs in a
        background task so app startup is never blocked. A no-op when disabled."""
        if not self._enabled:
            logger.info("web fetch: browser disabled")
            return
        if self._task is not None:  # already bringing up
            return
        logger.info("web fetch: bringing up the browser in the background")
        self._task = asyncio.create_task(self._bring_up())

    async def stop(self) -> None:
        """Cancel an in-flight bring-up, drop the CDP connection, and stop the browser.
        ``_bring_up`` logs its own failures, so the task only raises on the cancellation
        below."""
        await cancel_and_wait(self._task, STOP_TIMEOUT_S)
        self._task = None
        await self._disconnect()
        await self._stop_services()

    async def _stop_services(self) -> None:
        if self._vm is None:
            return
        try:
            await self._vm.stop_service(_FORWARDER)
            await self._vm.stop_service(_SERVICE)
        except VMError:
            logger.info("web fetch: could not stop the browser", exc_info=True)

    @asynccontextmanager
    async def context(self, url: str | None = None) -> AsyncIterator[BrowserContext]:
        """A fresh, isolated, stealthed browser context for one fetch — concurrency-bounded
        and always closed. When a cookie jar is enabled and ``url`` is given, the site's
        cached cookies are seeded in (so a prior challenge's clearance carries forward) and
        the context's cookies are harvested back out on exit. Raises ``RuntimeError`` if the
        browser isn't available (the fetcher checks :attr:`available` first and degrades)."""
        async with self._sem:
            if not self.available:
                raise RuntimeError("browser is not available")
            assert self._browser is not None
            ctx = await self._browser.new_context(
                accept_downloads=False,
                service_workers="block",
                **context_options(
                    user_agent=self._user_agent,
                    locale=self._locale,
                    timezone_id=self._timezone_id,
                ),
            )
            try:
                await ctx.add_init_script(INIT_SCRIPT)
                if self._jar is not None and url:
                    seed = self._jar.seed_for(url)
                    if seed:
                        try:
                            await ctx.add_cookies(seed)
                        except Exception:
                            # add_cookies is all-or-nothing — one malformed entry would drop
                            # the whole seed (clearance cookie included). Re-add one at a
                            # time so a single bad cookie doesn't defeat the carry-forward.
                            for cookie in seed:
                                try:
                                    await ctx.add_cookies([cookie])
                                except Exception:
                                    pass
                yield ctx
            finally:
                if self._jar is not None:
                    try:
                        self._jar.store(await ctx.cookies())
                    except Exception:
                        pass  # context already gone ⇒ nothing to harvest
                try:
                    await ctx.close()
                except Exception:
                    pass

    async def apply_stealth(self, page) -> None:
        """Bring a page's client hints + ``navigator.userAgentData`` into agreement with
        the spoofed UA (and strip the headless shell's ``HeadlessChrome`` brand) via a CDP
        user-agent override. Must run before the page's first navigation. Best-effort: a
        CDP hiccup degrades to UA-string-only stealth rather than failing an otherwise-fine
        fetch — the JS-surface evasions (init script) are unaffected either way."""
        try:
            cdp = await page.context.new_cdp_session(page)
            await cdp.send(
                "Emulation.setUserAgentOverride",
                user_agent_override(
                    user_agent=self._user_agent,
                    locale=self._locale,
                    browser_version=self._browser_version,
                ),
            )
        except Exception:
            logger.debug("web fetch: client-hint override failed; UA-string stealth only")

    async def _after_boot(self) -> None:
        """Start again on a VM that was reset: what ran in the old one is gone."""
        if self._task is None or not self._task.done():
            return
        await self._disconnect()
        self._task = asyncio.create_task(self._bring_up())

    async def _bring_up(self) -> None:
        vm = self._vm
        if vm is None:
            logger.info("web fetch: no platform VM — web fetch unavailable")
            return
        await vm.wait_ready(float("inf"))
        try:
            await self._stop_services()
            await vm.start_service(
                _SERVICE,
                _USER,
                [
                    _BINARY,
                    *LAUNCH_FLAGS,
                    f"--remote-debugging-port={_CDP_PORT}",
                    f"--proxy-server={vm.layout.platform_proxy}",
                    # Drops Chrome's implicit localhost bypass, so a page cannot reach the
                    # debugging port or anything else on the guest's loopback.
                    "--proxy-bypass-list=<-loopback>",
                    "--user-data-dir=/tmp/hephaestus-chromium",
                    # The guest kernel has no user namespaces, which Chromium's own
                    # sandbox needs; the service's confinement and its user stand in.
                    "--no-sandbox",
                ],
            )
            await vm.start_service(
                _FORWARDER,
                _USER,
                [
                    "python3",
                    f"{vm.layout.opt}/portfwd.py",
                    str(_CDP_EXPOSED),
                    str(_CDP_PORT),
                ],
            )
            host_port = await vm.forward(_CDP_EXPOSED)
        except VMError as exc:
            logger.warning("web fetch: browser did not start: %s", exc)
            return
        ws_url = await discover_cdp_ws(host_port, self._startup_timeout_s)
        if ws_url is None:
            logger.warning("web fetch: the browser's CDP endpoint did not become available")
            await self._stop_services()
            return
        try:
            pw = await async_playwright().start()
            self._pw = pw
            browser = await pw.chromium.connect_over_cdp(ws_url)
            self._browser = browser
        except Exception:
            logger.exception("web fetch: could not connect to the browser over CDP")
            await self._disconnect()
            await self._stop_services()
            return
        self._browser_version = browser.version
        self._user_agent = self._user_agent_override or realistic_user_agent(browser.version)
        self._ws_url = ws_url
        logger.info("web fetch: browser ready (Chromium %s)", browser.version)

    async def _disconnect(self) -> None:
        self._ws_url = None
        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception:
                pass
            self._browser = None
        if self._pw is not None:
            try:
                await self._pw.stop()
            except Exception:
                pass
            self._pw = None
