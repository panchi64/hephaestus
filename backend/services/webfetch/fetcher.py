"""BrowserFetcher — fetch one URL by rendering it in a headless browser, then extract
its main content as Markdown.

Replaces the old static httpx+trafilatura fetch: the page is loaded in Chromium so its
JavaScript runs and client-rendered content is present, then the rendered DOM is handed
to the extractor (:mod:`services.webfetch.extract`). The entry URL is pre-flighted
(:func:`core.ssrf.assert_public_url`) before a context opens, and every request the browser
then makes is gated by the VM's egress gateway (:mod:`services.vm.egress`). Output is
untrusted-wrapped — web content is data, never instructions.

Domain errors only (so the service stays reusable by non-agent callers): ``SSRFError`` for
a target refused by the pre-flight (hard boundary), ``WebFetchError`` for an
unreadable/unreachable page or a request the proxy refused mid-navigation (recoverable). The
tool layer maps these to a refusal message vs ``ModelRetry``.
"""

from __future__ import annotations

import asyncio
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Literal, Protocol

import httpx
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from core.citations import Citation
from core.exceptions import WebFetchError
from core.ssrf import assert_public_url
from core.text import tokens_to_chars, truncate_on_boundary
from core.untrusted import wrap_untrusted

from .distill import WebDistiller
from .extract import extract
from .pdf import fetch_pdf_text
from .throttle import DomainThrottle

WaitUntil = Literal["commit", "domcontentloaded", "load", "networkidle"]


class RenderResponse(Protocol):
    """The navigation response a fetch reads its status from."""

    @property
    def status(self) -> int: ...


class SnapshotPage(Protocol):
    """The slice of a Playwright page a fetch waits on and snapshots."""

    @property
    def url(self) -> str: ...

    async def wait_for_timeout(self, timeout: float) -> None: ...

    async def content(self) -> str: ...

    async def evaluate(self, expression: str) -> str: ...


class RenderPage(SnapshotPage, Protocol):
    """A snapshot page a fetch also navigates."""

    async def goto(
        self, url: str, *, wait_until: WaitUntil, timeout: float
    ) -> RenderResponse | None: ...


class RenderContext(Protocol):
    """The slice of a Playwright browser context a fetch opens its page from."""

    async def new_page(self) -> RenderPage: ...


class RenderBrowser(Protocol):
    """The slice of :class:`ManagedBrowser` a fetch renders through."""

    @property
    def available(self) -> bool: ...

    def context(self, url: str | None = None) -> AbstractAsyncContextManager[RenderContext]: ...

    async def apply_stealth(self, page: RenderPage) -> None: ...


class _DownloadStarted(Exception):
    """The browser began a file download instead of rendering a page (a `.pdf` URL and the
    like). Routed to the direct-download PDF path rather than surfaced as a render failure."""


_INNERTEXT_JS = "() => (document.body ? document.body.innerText : '')"

# An interstitial a bot wall serves while it runs its JS check (Cloudflare/DataDome/Reddit
# class). We can't *solve* a hostile challenge, but a real browser often clears a soft one
# on its own within a few seconds — so when the first render looks like one of these, we
# wait for it to settle and re-snapshot rather than returning the interstitial as content.
_CHALLENGE_MARKERS = (
    "just a moment",
    "checking your browser",
    "verify you are human",
    "verifying you are human",
    "attention required",
    "enable javascript and cookies to continue",
    "needs to review the security of your connection",
    "challenge-platform",
    "cf-chl",
)


# A bot wall often parks the browser on a challenge URL while its JS runs (Reddit appends
# ?js_challenge=1; Cloudflare uses /cdn-cgi/challenge + __cf_chl params). The challenge page
# itself can be near-empty, so the URL is the more reliable tell than its body text.
_CHALLENGE_URL_MARKERS = ("js_challenge=", "__cf_chl", "cf_chl_", "/cdn-cgi/challenge")


def looks_like_challenge(html: str, text: str) -> bool:
    """Whether a rendered page reads as a bot-wall interstitial rather than real content."""
    blob = (text or "")[:2000].lower()
    markup = (html or "")[:6000].lower()
    return any(m in blob or m in markup for m in _CHALLENGE_MARKERS)


def _is_challenge_url(url: str) -> bool:
    """Whether a URL is a bot wall's challenge endpoint (the browser was parked on it)."""
    low = (url or "").lower()
    return any(m in low for m in _CHALLENGE_URL_MARKERS)


@dataclass(frozen=True)
class FetchedPage:
    """A fetched page's main content as Markdown, untrusted-wrapped."""

    url: str
    title: str | None
    content: str

    def citations(self) -> list[Citation]:
        """The page itself — a fetch has exactly one source, and this is how the run
        stream learns of it without the event translator knowing what a fetch is.

        ``read``, and this is the rung the ladder exists for: the page was rendered and
        its text put in front of the model, which is a materially different claim from a
        search having listed it. A URL a search already surfaced is emitted a second time
        here, and the consumer's fold keeps this, the stronger sighting.
        """
        return [Citation(url=self.url, title=self.title, engagement="read")]


class BrowserFetcher:
    """Render-and-extract a single URL. The bounds default to the configured settings and
    are injected so tests can shrink them."""

    def __init__(
        self,
        *,
        browser: RenderBrowser,
        timeout_s: float = 15.0,
        wait_until: WaitUntil = "domcontentloaded",
        render_wait_ms: int = 250,
        max_bytes: int = 2_000_000,
        min_chars: int = 200,
        min_interval_s: float = 0.0,
        challenge_waits: int = 0,
        challenge_wait_ms: int = 5000,
        output_max_tokens: int = 4000,
        pdf_max_bytes: int = 25_000_000,
        pdf_max_pages: int = 50,
        http_client: httpx.AsyncClient | None = None,
        settle_checks: int = 3,
        settle_wait_ms: int = 750,
        settle_min_chars: int = 1000,
        distiller: WebDistiller | None = None,
    ) -> None:
        self._browser: RenderBrowser = browser
        self._timeout_ms: int = int(timeout_s * 1000)
        self._wait_until: WaitUntil = wait_until
        self._render_wait_ms: int = render_wait_ms
        self._max_bytes: int = max_bytes
        self._min_chars: int = min_chars
        self._throttle: DomainThrottle = DomainThrottle(min_interval_s=min_interval_s)
        self._challenge_waits: int = challenge_waits
        self._challenge_wait_ms: int = challenge_wait_ms
        self._output_max_tokens: int = output_max_tokens
        self._pdf_max_bytes: int = pdf_max_bytes
        self._pdf_max_pages: int = pdf_max_pages
        self._http_client: httpx.AsyncClient | None = http_client
        self._settle_checks: int = settle_checks
        self._settle_wait_ms: int = settle_wait_ms
        self._settle_min_chars: int = settle_min_chars
        self._distiller: WebDistiller | None = distiller

    async def fetch(
        self, _owner_id: str, url: str, *, offset: int = 0, goal: str | None = None
    ) -> FetchedPage:
        """Render ``url`` and return its main content as Markdown, capped to the output
        token budget (a longer page pages via ``offset``). With a ``goal`` (and no explicit
        ``offset``), an over-cap page is distilled to the goal-relevant content instead of
        truncated. Raises ``SSRFError`` (refused) or ``WebFetchError``
        (unreadable/unreachable/browser unavailable, or an ``offset`` past the end)."""
        # Pre-flight: a refused target never opens a browser context (keeps today's
        # synchronous SSRFError on the entry URL).
        await assert_public_url(url)
        if not self._browser.available:
            raise WebFetchError("web fetch is unavailable (headless browser not running)")
        # Per-host politeness: serialize same-site fetches with a minimum gap between them.
        async with self._throttle.slot(url):
            try:
                html, text, final_url = await self._render(url)
            except _DownloadStarted:
                # Not a page — Chromium began a download. Pull the bytes and read the PDF
                # text directly; the same output cap + offset paging applies.
                body = await fetch_pdf_text(
                    url,
                    timeout_s=self._timeout_ms / 1000,
                    max_bytes=self._pdf_max_bytes,
                    max_pages=self._pdf_max_pages,
                    client=self._http_client,
                )
                return await self._package(body, None, url, offset, goal=goal)
        title, body = await asyncio.to_thread(
            extract, html, url=final_url, rendered_text=text, min_chars=self._min_chars
        )
        if not body:
            raise WebFetchError(f"no readable content at {final_url!r}")
        return await self._package(body, title, final_url, offset, goal=goal)

    async def _package(
        self, body: str, title: str | None, final_url: str, offset: int, *, goal: str | None = None
    ) -> FetchedPage:
        """Cap ``body`` to the output token budget from ``offset``, wrap it untrusted, and —
        when more remains — append a trusted notice telling the model how to page on. An
        ``offset`` past the end raises ``WebFetchError`` so the tool layer feeds the model a
        retry to correct it.

        When ``goal`` is stated (and there's no explicit ``offset`` — an offset means the
        model wants raw paging) and the body is over the cap, the distiller reduces it to the
        goal-relevant content instead; a failed/absent distillation falls back to truncation."""
        total = len(body)
        if offset > 0 and offset >= total:
            raise WebFetchError(
                f"offset {offset} is past the end of the content ({total} characters)"
            )
        cap_chars = tokens_to_chars(self._output_max_tokens)
        if goal and offset == 0 and total > cap_chars and self._distiller is not None:
            digest = await self._distiller.distill(body, goal=goal, url=final_url)
            if digest is not None:
                digest = truncate_on_boundary(digest, cap_chars)
                content = wrap_untrusted(digest, source=final_url)
                content += (
                    f"\n[The page's full text is {total} characters; this result is a "
                    f"distillation focused on: {goal}. To read the raw text instead, call "
                    "this tool again without a goal and page through it with offset.]"
                )
                return FetchedPage(url=final_url, title=title, content=content)
        capped = truncate_on_boundary(body[offset:], cap_chars)
        end = offset + len(capped)
        content = wrap_untrusted(capped, source=final_url)
        if end < total:
            content += (
                f"\n[Fetched content truncated: showing characters {offset}-{end} of {total}. "
                "To continue reading, call this tool again with the same url and "
                f"offset={end}.]"
            )
        return FetchedPage(url=final_url, title=title, content=content)

    async def _render(self, url: str) -> tuple[str, str, str]:
        """Load the page in an isolated context and capture the rendered HTML, the rendered
        innerText, and the final URL. SSRF is enforced out-of-browser by the VM's egress gateway
        (it refuses non-public destinations on every request, including redirects); a request
        it refuses surfaces here as a load failure, which degrades to a recoverable error —
        the synchronous pre-flight in :meth:`fetch` is what yields the hard SSRF refusal."""
        try:
            # Pass the URL so the site's cached cookies (a prior challenge's clearance) seed
            # this context and any it gains are harvested back when it closes.
            async with self._browser.context(url) as ctx:
                page = await ctx.new_page()
                await self._browser.apply_stealth(page)  # client hints, before any nav
                response = None
                try:
                    response = await page.goto(
                        url, wait_until=self._wait_until, timeout=self._timeout_ms
                    )
                except PlaywrightTimeoutError:
                    pass  # use whatever rendered within the budget
                except PlaywrightError as exc:
                    # A `.pdf` (and other downloadable) URL makes Chromium start a download
                    # rather than render — route it to the direct-download PDF path.
                    if "download is starting" in str(exc).lower():
                        raise _DownloadStarted from exc
                    raise WebFetchError(f"could not load {url!r}: {_reason(exc)}") from exc
                if response is not None and response.status >= 400:
                    raise WebFetchError(f"{page.url!r} returned HTTP {response.status}")
                final_url, html, text = await self.capture(url, page)
        except WebFetchError:
            raise
        except RuntimeError as exc:
            # The browser dropped between fetch()'s availability check and acquiring a
            # context — degrade rather than leak an unhandled error to the tool.
            raise WebFetchError(f"web fetch is unavailable: {exc}") from exc
        except PlaywrightError as exc:
            raise WebFetchError(f"could not render {url!r}: {_reason(exc)}") from exc
        # Cap (in characters) what we hand the extractor. The browser already buffered the
        # full page; this just keeps an outsized DOM
        # from ballooning the extractor's input. trafilatura/lxml recover from a mid-tag cut.
        return html[: self._max_bytes], (text or "")[: self._max_bytes], final_url

    async def capture(self, url: str, page: SnapshotPage) -> tuple[str, str, str]:
        """Snapshot the page, and if it reads as a bot-wall interstitial, wait for it to
        clear and re-snapshot — up to ``challenge_waits`` times. A real browser often
        settles a soft challenge on its own within a few seconds; this stops us returning
        the 'Just a moment…' page as if it were the content (it can't beat a hard wall)."""
        final_url, html, text = await self._snapshot(url, page)
        for _ in range(self._challenge_waits):
            # A challenge URL alone isn't enough to keep waiting — some walls keep their
            # param in the URL after the real content loads; only wait while content is thin.
            thin = len((text or "").strip()) < self._min_chars
            if not (looks_like_challenge(html, text) or (_is_challenge_url(final_url) and thin)):
                break
            try:
                await page.wait_for_timeout(self._challenge_wait_ms)
            except PlaywrightError:
                break  # page/context went away mid-wait — return what we have
            try:
                final_url, html, text = await self._snapshot(url, page)
            except WebFetchError:
                break  # a re-snapshot that won't settle ⇒ keep the prior best-effort capture
        # Adaptive settle: a JS-heavy page (SPA) can report its shell first and fill in the
        # real content a beat later. Re-snapshot while the page is still thin or is visibly
        # still growing (>20% since the last look), up to settle_checks times. A page whose
        # first snapshot is already rich and stable pays zero extra waits.
        prev = -1
        for _ in range(self._settle_checks):
            cur = len((text or "").strip())
            still_thin = cur < self._settle_min_chars
            still_growing = prev >= 0 and cur > prev * 1.2
            if not (still_thin or still_growing):
                break
            try:
                await page.wait_for_timeout(self._settle_wait_ms)
            except PlaywrightError:
                break  # page/context went away mid-wait — return what we have
            prev = cur
            try:
                final_url, html, text = await self._snapshot(url, page)
            except WebFetchError:
                break  # a re-snapshot that won't settle ⇒ keep the prior best-effort capture
        return final_url, html, text

    async def _snapshot(self, url: str, page: SnapshotPage) -> tuple[str, str, str]:
        """Settle, then snapshot the final URL + rendered HTML + innerText — bounded by the
        timeout so a page whose JS never quiesces can't hold the fetch open indefinitely
        (content()/evaluate() don't honour the navigation timeout)."""

        async def take() -> tuple[str, str, str]:
            if self._render_wait_ms:
                await page.wait_for_timeout(self._render_wait_ms)
            html = await page.content()
            text = await page.evaluate(_INNERTEXT_JS)
            return page.url, html, text

        try:
            return await asyncio.wait_for(take(), timeout=self._timeout_ms / 1000)
        except TimeoutError as exc:
            raise WebFetchError(f"render did not settle for {url!r}") from exc


def _reason(exc: Exception) -> str:
    """Playwright errors are multi-line and verbose — take the first line for the model."""
    first = str(exc).splitlines()
    return first[0] if first else exc.__class__.__name__
