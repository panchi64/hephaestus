"""HostBrowser — the visible Chromium the agent drives and the operator shares.

The browse tools used to attach to web fetch's containerized headless Chromium, which
made the agent's browsing invisible: the operator could watch a screencast of it but
could not *touch* it. Yet the two things an operator most wants from an agent's browser
are exactly the ones that need hands — do this one step faster than describing it, and
log me in before you start. So this browser is a real window on the operator's own
machine, launched by Playwright with ``headless=False``, and the agent reaches the same
window over CDP. Web fetch keeps its container untouched; this is a second browser, and
the only one page JavaScript from an agent-driven page runs in.

**Launched lazily.** ``ensure()`` is called from the session manager's attach path, so no
window appears at boot and a thread that never browses never opens one.

**The SSRF boundary survives the move to the host.** The same stdlib-only proxy web
fetch's sidecar runs (``services/webfetch/proxy_script.py``) is spawned here as a plain
host subprocess on loopback, and Chromium is pointed at it with ``--proxy-server`` plus
``--proxy-bypass-list=<-loopback>`` — the bypass list is what closes the hole a host
browser would otherwise open, since without it the window could reach this very backend
and every other service on the operator's loopback. Fail-closed: no proxy, no browser.
The cost is real and deliberate — a loopback dev server is unreachable in this window.

**Chromium's own sandbox is the rest of the boundary**, and it is asked for explicitly at
launch rather than left to Playwright's default, which is off. With no container under
this browser, the renderer sandbox is all that separates a page the model chose from the
operator's account.

**``cdp_url`` is the liveness answer, not a stored string.** It is ``None`` unless the
browser handle is still connected *and* the proxy is still running, so an operator who
quits Chromium (or a proxy that died) turns every session attached to it into one the
manager's next sweep reaps, and the acquire after that launches a fresh window.

The driver, the proxy spawn and the endpoint discovery are injectable for one reason: a
test may assert what we launch Chromium with, and must never launch Chromium.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
import sys
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from services.webfetch.browser import PROXY_SCRIPT
from services.webfetch.cdp import discover_cdp_ws
from services.webfetch.stealth import INIT_SCRIPT, LAUNCH_FLAGS

logger = logging.getLogger(__name__)

#: How long the proxy's own shutdown may take before it is left to the OS. It is a
#: loopback socket server with no state to flush, so this is generous already.
_PROXY_STOP_TIMEOUT_S = 5.0
_BROWSER_CLOSE_TIMEOUT_S = 5.0

#: Starts the Playwright driver and launches a browser with ``args``, returning both so
#: each can be torn down. A test seam (see the module docstring), never a policy knob.
type Launcher = Callable[[Sequence[str]], Awaitable[tuple[Any, Any]]]
#: Spawns ``argv`` and returns something process-shaped: ``stdout``, ``returncode``,
#: ``terminate()``, ``wait()``.
type Spawner = Callable[[Sequence[str]], Awaitable[Any]]
#: Resolves a debugging port to a CDP websocket URL, or ``None`` on a deadline.
type Discoverer = Callable[[int, float], Awaitable[str | None]]


def _free_port() -> int:
    """A loopback port nothing is listening on, by binding zero and letting go.

    Racy by construction — something else may take it between the close and the launch —
    and the alternative (a fixed port) is worse: two ports fixed in the source would
    collide with whatever the operator already runs there, on their own machine, every
    time. A lost race fails the launch, which is already a degrade path.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _launch_args(cdp_port: int, proxy_port: int) -> list[str]:
    """What the window is launched with: the same disguise and proxy-tightening flags web
    fetch's container uses, plus the debugging port we then attach over.

    ``LAUNCH_FLAGS`` is shared rather than re-listed because two of its entries are part
    of the egress boundary, not cosmetics: QUIC goes straight to the origin over UDP
    instead of through an HTTP proxy, and WebRTC will happily send UDP to a host a page
    chooses. Both would step around the proxy in *this* browser exactly as they would in
    the container.
    """
    return [
        *LAUNCH_FLAGS,
        f"--remote-debugging-port={cdp_port}",
        f"--proxy-server=127.0.0.1:{proxy_port}",
        # Drops Chrome's implicit localhost bypass, so the page cannot reach the
        # operator's own loopback services (this backend included) around the proxy.
        "--proxy-bypass-list=<-loopback>",
    ]


async def _launch_chromium(args: Sequence[str]) -> tuple[Any, Any]:
    """Start a Playwright driver and a headed Chromium on it.

    ``chromium_sandbox`` is spelled out because Playwright's own default is the unsafe
    one: left unset, it appends ``--no-sandbox`` and every page the agent opens renders
    with the renderer sandbox off, as the operator, on the operator's filesystem. There is
    no container under this browser any more, so Chromium's sandbox is the whole of what
    stands between an arbitrary page and that filesystem.

    Imported lazily so a process that never opens a browser never pays for the driver
    module, and so tests that inject a launcher need no Playwright at all.
    """
    from playwright.async_api import async_playwright

    driver = await async_playwright().start()
    try:
        browser = await driver.chromium.launch(
            headless=False, chromium_sandbox=True, args=list(args)
        )
    except Exception:
        with contextlib.suppress(Exception):
            await driver.stop()
        raise
    return driver, browser


async def _spawn_proxy(argv: Sequence[str]) -> Any:
    """Run the SSRF proxy as a host subprocess: fixed argv, no shell.

    ``stdout`` is a pipe because the readiness line arrives on it; ``stderr`` is discarded
    rather than piped, since nothing drains a second pipe and a full one would wedge the
    proxy mid-request.
    """
    return await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )


async def _await_proxy_ready(proc: Any, timeout_s: float) -> bool:
    """Wait for the proxy's readiness line, and treat anything else as not ready.

    The proxy prints ``PROXY-READY`` once it is listening and then nothing, so this reads
    at most one line and leaves the pipe alone afterwards. A closed stream means it exited
    before ever listening — the one failure that must not be read as success, because a
    browser pointed at a dead proxy would be a browser with no egress guard at all.
    """
    stdout = getattr(proc, "stdout", None)
    if stdout is None:
        return False
    try:
        async with asyncio.timeout(timeout_s):
            while True:
                line = await stdout.readline()
                if not line:
                    return False
                if b"PROXY-READY" in line:
                    return True
    except (TimeoutError, OSError):
        return False


class HostBrowser:
    """One visible Chromium and the SSRF proxy it is pointed at, brought up on demand."""

    def __init__(
        self,
        *,
        launch_timeout_s: float = 30.0,
        launcher: Launcher | None = None,
        spawner: Spawner | None = None,
        discoverer: Discoverer | None = None,
    ) -> None:
        self._launch_timeout_s = launch_timeout_s
        self._launcher = launcher or _launch_chromium
        self._spawner = spawner or _spawn_proxy
        self._discoverer = discoverer or discover_cdp_ws
        self._driver: Any | None = None
        self._browser: Any | None = None
        self._proxy: Any | None = None
        self._ws_url: str | None = None
        # One launch at a time: several browse tools inside one model response race the
        # first acquire of a thread, and two of them here would be two windows.
        self._lock = asyncio.Lock()

    @property
    def cdp_url(self) -> str | None:
        """Where to attach, or ``None`` when there is nothing safe to attach to.

        Both halves are load-bearing. A disconnected handle is an operator who closed the
        window; a dead proxy is a browser whose egress is no longer guarded, and handing
        out the endpoint then would be handing out an unguarded browser.
        """
        browser = self._browser
        if browser is None or not browser.is_connected():
            return None
        if self._proxy is None or self._proxy.returncode is not None:
            return None
        return self._ws_url

    async def ensure(self) -> str | None:
        """The CDP endpoint of a running window, launching one if there is none.

        Returns ``None`` when the launch failed, which is a degraded capability rather
        than an error: the caller tells the model there is no browser, exactly as web
        fetch degrades when its container cannot come up.
        """
        async with self._lock:
            live = self.cdp_url
            if live is not None:
                return live
            # Something is half-alive: a quit window whose driver is still up, or a proxy
            # that outlived the browser. Clear it before spending a new pair of ports.
            await self._teardown()
            try:
                return await self._launch()
            except Exception:
                logger.warning("browser: the host browser could not be launched", exc_info=True)
                await self._teardown()
                return None

    async def _launch(self) -> str | None:
        proxy_port, cdp_port = _free_port(), _free_port()
        self._proxy = await self._spawner([sys.executable, str(PROXY_SCRIPT), str(proxy_port)])
        if not await _await_proxy_ready(self._proxy, self._launch_timeout_s):
            logger.warning("browser: the SSRF proxy did not come up — no window was opened")
            await self._teardown()
            return None
        self._driver, self._browser = await self._launcher(_launch_args(cdp_port, proxy_port))
        self._ws_url = await self._discoverer(cdp_port, self._launch_timeout_s)
        if self._ws_url is None:
            logger.warning("browser: the window's CDP endpoint never became available")
            await self._teardown()
            return None
        logger.info("browser: opened a window on the host, guarded by its own SSRF proxy")
        return self.cdp_url

    async def stop(self) -> None:
        """Close the window and the proxy. Registered on the app's lifecycle, so shutting
        the backend down does not leave a browser the operator has to find and quit."""
        async with self._lock:
            await self._teardown()

    async def close_if_dead(self) -> None:
        """Drop what a quit window left behind — its driver and, above all, its proxy —
        unless a launch has happened since the caller looked. The re-check runs under the
        lock, so a fresh window is never torn down by a sweep that saw the old one die."""
        async with self._lock:
            if self.cdp_url is None:
                await self._teardown()

    async def apply_stealth(self, page: Any) -> None:
        """Mask the automation tells (``navigator.webdriver`` and friends) on a page.

        The client-hint user-agent override web fetch also applies is deliberately not
        here: this is a real desktop Chromium whose hints already describe a real desktop
        Chromium, and overriding them would introduce the very mismatch it exists to
        remove.
        """
        await page.add_init_script(INIT_SCRIPT)

    async def _teardown(self) -> None:
        """Drop everything the launch built, in the order that a half-built one survives.
        Called under the lock; never raises."""
        self._ws_url = None
        browser, self._browser = self._browser, None
        driver, self._driver = self._driver, None
        proxy, self._proxy = self._proxy, None
        if browser is not None:
            # Bounded: closing over CDP to a Chromium that is hung (or already gone) never
            # answers, and this runs inside app shutdown.
            with contextlib.suppress(Exception):
                await asyncio.wait_for(browser.close(), timeout=_BROWSER_CLOSE_TIMEOUT_S)
        if driver is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(driver.stop(), timeout=_BROWSER_CLOSE_TIMEOUT_S)
        if proxy is not None and proxy.returncode is None:
            with contextlib.suppress(Exception):
                proxy.terminate()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proxy.wait(), timeout=_PROXY_STOP_TIMEOUT_S)
