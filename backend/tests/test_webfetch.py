"""Web fetch: the render-and-extract pipeline.

Four layers, fastest-first:
- ``extract`` — the trafilatura→innerText cascade over an HTML string (no browser).
- ``RequestGuard`` — the per-request SSRF policy (no browser; IP literals stay offline).
- ``BrowserFetcher`` — pre-flight refusal + degrade, and a real Chromium render in the
  platform VM that proves JavaScript-injected content is captured (skipped without a real VM).
- the agent reaching ``web_fetch`` through the toolset stack.
"""

from __future__ import annotations

import asyncio
import os
import urllib.parse
from collections.abc import AsyncGenerator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from pathlib import Path
from typing import cast, final, override

import httpx
import pytest
from playwright.async_api import Page, Route
from pydantic_ai import ModelResponse, TextPart
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models import Model
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.settings import ModelSettings

from core.config import Settings
from core.exceptions import SSRFError, WebFetchError
from core.text import tokens_to_chars, truncate_on_boundary
from services.vm import PlatformVM
from services.vm.smolvm import SmolvmDriver
from services.webfetch import BrowserFetcher, FetchedPage, ManagedBrowser
from services.webfetch.cookies import DomainCookieJar
from services.webfetch.distill import WebDistiller
from services.webfetch.extract import extract
from services.webfetch.fetcher import (
    RenderBrowser,
    RenderContext,
    RenderPage,
    RenderResponse,
    SnapshotPage,
    WaitUntil,
    looks_like_challenge,
)
from services.webfetch.throttle import DomainThrottle

OWNER = "operator"

# A page shaped like a real one — main content wrapped in the boilerplate (nav, cookie
# banner, sidebar ad, footer) every site carries. Deliberately not a bare <article>: the
# extractor's job is both to *keep* the structure below and to *drop* the chrome around
# it, and a fixture with nothing to prune tests only half of that. It also has to clear
# the size at which trafilatura falls back to plain text recovery — under that, no
# extractor emits Markdown and the assertions below would pass or fail for the wrong
# reason.
_ARTICLE = (
    "<html><head><title>Edible Plants</title></head><body>"
    '<div class="cookie-banner">We use cookies to improve your experience.</div>'
    '<nav><a href="/">Home</a><a href="/pricing">Pricing</a></nav>'
    "<article>"
    "<h1>Foraging Guide</h1>"
    "<p>The dandelion is entirely edible, from root to flower, and grows almost "
    "everywhere people live, which makes it a reliable first plant to learn. Its "
    "leaves are best picked young, before the plant flowers and bitterness sets in.</p>"
    "<h2>Identification</h2>"
    "<p>Look for the <b>basal rosette</b> of toothed leaves and a single hollow stem "
    "carrying one flower head. A milky sap runs from any broken part of the plant.</p>"
    "<ul><li>Leaves: deeply toothed</li><li>Stem: hollow</li><li>Sap: milky</li></ul>"
    "<p>Identify with <code>field_guide.check(plant)</code> before eating anything.</p>"
    "<table><tr><th>Part</th><th>Use</th></tr><tr><td>root</td><td>roasted</td></tr>"
    "<tr><td>leaf</td><td>salad</td></tr></table>"
    "<p>Always positively identify a plant before eating any part of it in the wild. "
    'See the <a href="https://example.org/guide">full guide</a>.</p>'
    "</article>"
    '<aside class="ad">Sponsored: buy our thing today.</aside>'
    "<footer>Copyright 2026 Forage Co.</footer>"
    "</body></html>"
)


# --- extract cascade (no browser) ------------------------------------------


def test_extract_article_to_markdown_with_table():
    title, body = extract(_ARTICLE, url="https://forage.example/g", rendered_text="x", min_chars=50)
    assert body is not None
    assert "dandelion" in body
    assert title == "Edible Plants" or "Foraging" in (title or "")
    # Every structure the extractor promises to preserve, pinned individually — asserting
    # only one of them lets a silent formatting regression through while the test passes.
    assert "# Foraging Guide" in body  # headings, as Markdown rather than bare text
    assert "**basal rosette**" in body  # inline formatting
    assert "- Leaves: deeply toothed" in body  # list items
    assert "`field_guide.check(plant)`" in body  # inline code
    assert "| root | roasted |" in body  # table rows
    assert "](https://example.org/guide)" in body  # links keep their destination


def test_extract_prunes_boilerplate_around_the_article():
    # The other half of extraction: the chrome every real page carries must not survive
    # into what the model reads.
    _title, body = extract(
        _ARTICLE, url="https://forage.example/g", rendered_text="x", min_chars=50
    )
    assert body is not None
    for chrome in ("cookie", "Sponsored", "Copyright", "Pricing"):
        assert chrome.lower() not in body.lower()


def test_extract_falls_back_to_innertext_when_thin():
    # A page with no main-content prose: trafilatura finds little, so the rendered
    # innerText carries the result instead.
    thin = "<html><body><nav>home about contact</nav></body></html>"
    fallback = "the actual visible text a reader would see on this page, plainly rendered"
    _title, body = extract(thin, url="https://x.example", rendered_text=fallback, min_chars=500)
    assert body is not None
    assert fallback in body


def test_extract_prefers_innertext_when_extractor_output_is_thin_fraction():
    # A small extractable block sits inside a page whose visible text is far richer — the
    # main-content heuristic under-selected, so innerText (by length) should win.
    prose = "A short paragraph about dandelions. " * 20  # ~720 chars
    html = f"<html><body><article><h1>T</h1><p>{prose}</p></article></body></html>"
    rendered = "UNIQUEINNERTEXTMARKER " + ("visible rendered spec-table content. " * 800)  # ~30k
    _title, body = extract(html, url="https://x.example", rendered_text=rendered, min_chars=200)
    assert body is not None
    assert "UNIQUEINNERTEXTMARKER" in body  # innerText fallback carried the result


def test_extract_keeps_extractor_when_ratio_healthy():
    # A normal article: the extractor captures most of the visible text, so its Markdown
    # (table preserved as pipes) wins even though innerText clears the floor.
    prose = "The dandelion is entirely edible and grows widely. " * 200  # ~10k chars
    html = (
        f"<html><body><article><h1>Foraging Guide</h1><p>{prose}</p>"
        "<table><tr><td>root</td><td>edible</td></tr></table></article></body></html>"
    )
    rendered = prose + " home about contact"  # long innerText, no table pipes
    _title, body = extract(html, url="https://x.example", rendered_text=rendered, min_chars=200)
    assert body is not None
    assert "|" in body  # the Markdown table survived → the extractor output won, not innerText


def test_extract_keeps_substantial_article_on_comment_heavy_page():
    # A clean, substantial article (well over the innerText floor) whose page also carries a
    # large comment/nav section: the article is only a small *fraction* of the rendered
    # innerText, but it is exactly the content we want — it must win outright, not be demoted
    # so raw innerText (nav/comment noise, table flattened) can beat it by length.
    prose = "The dandelion is entirely edible and grows widely across the region. " * 80  # ~5.4k
    html = (
        f"<html><body><article><h1>Foraging Guide</h1><p>{prose}</p>"
        "<table><tr><td>root</td><td>edible</td></tr></table></article></body></html>"
    )
    rendered = prose + (" NOISE comment nav advert boilerplate. " * 2000)  # ~80k of noise
    _title, body = extract(html, url="https://x.example", rendered_text=rendered, min_chars=200)
    assert body is not None
    assert "|" in body  # the substantial Markdown extraction won, not the raw innerText


def test_extract_empty_returns_none():
    _title, body = extract(
        "<html><body></body></html>", url="https://x.example", rendered_text="", min_chars=50
    )
    assert body is None


# --- cookie jar: per-domain, TTL'd, bounded (no browser) -------------------


def test_cookie_jar_seeds_by_host_suffix():
    jar = DomainCookieJar(ttl_s=100, max_entries=10)
    jar.store([{"name": "sess", "value": "1", "domain": ".reddit.com", "path": "/"}])
    assert [c.get("name") for c in jar.seed_for("https://www.reddit.com/r/x")] == ["sess"]
    assert jar.seed_for("https://example.com/") == []  # different site → nothing


def test_cookie_jar_drops_source_expired_cookie():
    jar = DomainCookieJar(ttl_s=100, max_entries=10)
    jar.store([{"name": "old", "value": "1", "domain": "x.com", "path": "/", "expires": 1.0}])
    assert jar.seed_for("https://x.com/") == []  # already past its own expiry


def test_cookie_jar_later_set_overwrites_and_caps():
    jar = DomainCookieJar(ttl_s=100, max_entries=2)
    jar.store([{"name": "a", "value": "1", "domain": "x.com", "path": "/"}])
    jar.store([{"name": "a", "value": "2", "domain": "x.com", "path": "/"}])  # same key
    jar.store([{"name": "b", "value": "1", "domain": "x.com", "path": "/"}])
    jar.store([{"name": "c", "value": "1", "domain": "x.com", "path": "/"}])  # evicts oldest (a)
    got = {c.get("name"): c.get("value") for c in jar.seed_for("https://x.com/")}
    assert got == {"b": "1", "c": "1"}


def test_cookie_jar_evicts_on_ttl(monkeypatch: pytest.MonkeyPatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr("services.webfetch.cookies.time.monotonic", lambda: clock["t"])
    jar = DomainCookieJar(ttl_s=10, max_entries=10)
    jar.store([{"name": "s", "value": "1", "domain": "x.com", "path": "/"}])
    clock["t"] += 5
    assert [c.get("name") for c in jar.seed_for("https://x.com/")] == ["s"]  # within TTL
    clock["t"] += 10
    assert jar.seed_for("https://x.com/") == []  # past TTL → evicted


# --- per-domain throttle (no browser) --------------------------------------


async def test_throttle_spaces_same_host():
    throttle = DomainThrottle(min_interval_s=0.05)
    start = asyncio.get_event_loop().time()
    async with throttle.slot("https://x.com/a"):
        pass
    async with throttle.slot("https://x.com/b"):
        pass
    assert asyncio.get_event_loop().time() - start >= 0.05  # second waited out the gap


async def test_throttle_does_not_block_distinct_hosts():
    throttle = DomainThrottle(min_interval_s=10.0)
    start = asyncio.get_event_loop().time()
    async with throttle.slot("https://a.com/"):
        pass
    async with throttle.slot("https://b.com/"):  # different host → no wait
        pass
    assert asyncio.get_event_loop().time() - start < 1.0


async def test_throttle_disabled_is_noop():
    async with DomainThrottle(min_interval_s=0.0).slot("https://a.com/"):
        pass  # never waits, never raises


async def test_throttle_prune_keeps_a_locked_host(monkeypatch: pytest.MonkeyPatch):
    # At the tracking cap, pruning must skip a host whose lock is held — else two same-host
    # fetches would acquire different locks and run in parallel (the race the throttle prevents).
    monkeypatch.setattr("services.webfetch.throttle._MAX_TRACKED", 1)
    throttle = DomainThrottle(min_interval_s=0.01)
    release = asyncio.Event()

    async def hold_a() -> None:
        async with throttle.slot("https://a.com/"):
            _ = await release.wait()

    task = asyncio.create_task(hold_a())
    await asyncio.sleep(0.05)  # let it acquire a.com's slot
    async with throttle.slot("https://b.com/"):  # at cap ⇒ triggers a prune
        pass
    assert throttle.tracks("a.com")  # locked host survived the prune
    release.set()
    await task


# --- challenge-interstitial detection (no browser) -------------------------


def test_detects_challenge_interstitial():
    assert looks_like_challenge("<title>Just a moment...</title>", "Checking your browser")
    assert looks_like_challenge("<div class='challenge-platform'></div>", "")


def test_normal_page_is_not_a_challenge():
    assert not looks_like_challenge("<h1>Guide</h1>", "Real content about foraging dandelions")


# --- BrowserFetcher: pre-flight + degrade (no browser) ---------------------


async def test_fetch_refuses_private_target_before_opening_browser():
    # enabled=False ⇒ never started; the pre-flight SSRF check must fire first anyway.
    fetcher = BrowserFetcher(
        browser=ManagedBrowser(
            enabled=False,
            vm=None,
            startup_timeout_s=1.0,
            concurrency=1,
            user_agent="t",
            locale="en-US",
            timezone_id="UTC",
        )
    )
    with pytest.raises(SSRFError):
        _ = await fetcher.fetch(OWNER, "http://10.0.0.1/admin")


async def test_fetch_degrades_when_browser_unavailable():
    fetcher = BrowserFetcher(
        browser=ManagedBrowser(
            enabled=False,
            vm=None,
            startup_timeout_s=1.0,
            concurrency=1,
            user_agent="t",
            locale="en-US",
            timezone_id="UTC",
        )
    )
    # A public IP literal passes the pre-flight offline; the unavailable browser then degrades.
    with pytest.raises(WebFetchError):
        _ = await fetcher.fetch(OWNER, "http://93.184.216.34/")


async def _unavailable_context() -> RenderContext:
    raise RuntimeError("browser is not available")


@final
class _DroppingBrowser(RenderBrowser):
    """available reports True, but acquiring a context fails — models the browser dropping
    between the availability check and context entry (a TOCTOU disconnect)."""

    @property
    @override
    def available(self) -> bool:
        return True

    @override
    @asynccontextmanager
    async def context(self, url: str | None = None) -> AsyncGenerator[RenderContext]:
        yield await _unavailable_context()

    @override
    async def apply_stealth(self, page: RenderPage) -> None:
        raise AssertionError("unexpected call: apply_stealth")


async def test_fetch_maps_browser_dropout_to_webfetch_error():
    # Public IP literal passes the pre-flight and the available-check; context() then raises
    # RuntimeError. It must surface as a recoverable WebFetchError, not an unhandled error
    # the tool can't translate.
    fetcher = BrowserFetcher(browser=_DroppingBrowser())
    with pytest.raises(WebFetchError):
        _ = await fetcher.fetch(OWNER, "http://93.184.216.34/")


# --- output cap + offset paging (no browser) -------------------------------


@final
class _AvailableBrowser(RenderBrowser):
    """Reports available; never opens a context — the render is patched out so these
    tests exercise the cap/offset packaging on a synthetic oversized body."""

    @property
    @override
    def available(self) -> bool:
        return True

    @override
    def context(self, url: str | None = None) -> AbstractAsyncContextManager[RenderContext]:
        raise AssertionError("unexpected call: context")

    @override
    async def apply_stealth(self, page: RenderPage) -> None:
        raise AssertionError("unexpected call: apply_stealth")


_BIG_BODY = ("word " * 20_000).strip()  # ~100k chars of predictable text


def _fetcher_with_body(
    monkeypatch: pytest.MonkeyPatch,
    body: str,
    *,
    output_max_tokens: int = 4000,
    distiller: WebDistiller | None = None,
) -> BrowserFetcher:
    fetcher = BrowserFetcher(
        browser=_AvailableBrowser(),
        output_max_tokens=output_max_tokens,
        distiller=distiller,
    )

    async def _fake_render(url: str) -> tuple[str, str, str]:
        # html="" so the extractor falls back to the rendered innerText verbatim, giving
        # a body of exactly the length we control.
        return "", body, url

    monkeypatch.setattr(fetcher, "_render", _fake_render)
    return fetcher


async def test_fetch_caps_output_and_appends_truncation_note(monkeypatch: pytest.MonkeyPatch):
    fetcher = _fetcher_with_body(monkeypatch, _BIG_BODY)
    page = await fetcher.fetch(OWNER, "http://93.184.216.34/")

    cap = tokens_to_chars(4000)
    expected_capped = truncate_on_boundary(_BIG_BODY, cap)
    end = len(expected_capped)
    assert len(expected_capped) <= cap
    assert expected_capped in page.content
    # The notice is trusted (outside the fence) — it must come *after* the END marker.
    assert '</untrusted id="' in page.content
    assert page.content.index("[Fetched content truncated") > page.content.index('</untrusted id="')
    assert f"characters 0-{end} of {len(_BIG_BODY)}" in page.content
    assert f"offset={end}" in page.content


async def test_fetch_offset_returns_next_window(monkeypatch: pytest.MonkeyPatch):
    fetcher = _fetcher_with_body(monkeypatch, _BIG_BODY)
    cap = tokens_to_chars(4000)
    first = truncate_on_boundary(_BIG_BODY, cap)
    end = len(first)

    page = await fetcher.fetch(OWNER, "http://93.184.216.34/", offset=end)
    expected_next = truncate_on_boundary(_BIG_BODY[end:], cap)
    assert expected_next in page.content
    assert first not in page.content  # no overlap back into the previous window

    # The final window (remaining < cap) carries no truncation notice.
    tail_offset = len(_BIG_BODY) - 40
    last = await fetcher.fetch(OWNER, "http://93.184.216.34/", offset=tail_offset)
    assert _BIG_BODY[tail_offset:] in last.content
    assert "Fetched content truncated" not in last.content


async def test_fetch_offset_past_end_raises(monkeypatch: pytest.MonkeyPatch):
    fetcher = _fetcher_with_body(monkeypatch, _BIG_BODY)
    with pytest.raises(WebFetchError):
        _ = await fetcher.fetch(OWNER, "http://93.184.216.34/", offset=10**9)


# --- goal-aware distillation (no browser) ----------------------------------


async def _no_model() -> tuple[Model, ModelSettings | None]:
    raise AssertionError("unexpected call: resolve_model")


@final
class _FakeDistiller(WebDistiller):
    """A stand-in distiller returning a fixed result and counting its calls."""

    def __init__(self, result: str | None) -> None:
        super().__init__(
            resolve_model=_no_model,
            instructions="",
            window_tokens=1,
            max_windows=1,
            timeout_s=1.0,
        )
        self.result: str | None = result
        self.calls: int = 0

    @override
    async def distill(self, body: str, *, goal: str, url: str) -> str | None:
        self.calls += 1
        return self.result


async def test_fetch_distills_when_goal_given_and_body_over_cap(monkeypatch: pytest.MonkeyPatch):
    distiller = _FakeDistiller("DISTILLED — RAV4 trim price: $38,795")
    fetcher = _fetcher_with_body(monkeypatch, _BIG_BODY, distiller=distiller)
    page = await fetcher.fetch(OWNER, "http://93.184.216.34/", goal="RAV4 prices")
    assert distiller.calls == 1
    assert "DISTILLED — RAV4 trim price: $38,795" in page.content
    assert '<untrusted id="' in page.content  # still untrusted-fenced
    assert "distillation focused on: RAV4 prices" in page.content
    assert "Fetched content truncated" not in page.content  # distilled, not truncated


async def test_fetch_goal_ignored_when_body_under_cap(monkeypatch: pytest.MonkeyPatch):
    distiller = _FakeDistiller("unused")
    fetcher = _fetcher_with_body(monkeypatch, "a small page body", distiller=distiller)
    page = await fetcher.fetch(OWNER, "http://93.184.216.34/", goal="anything")
    assert distiller.calls == 0  # under the cap → no distillation
    assert "a small page body" in page.content


async def test_fetch_offset_wins_over_goal(monkeypatch: pytest.MonkeyPatch):
    distiller = _FakeDistiller("unused")
    fetcher = _fetcher_with_body(monkeypatch, _BIG_BODY, distiller=distiller)
    page = await fetcher.fetch(OWNER, "http://93.184.216.34/", offset=100, goal="x")
    assert distiller.calls == 0  # an explicit offset means raw paging — distiller skipped
    assert "Fetched content truncated" in page.content


async def test_fetch_falls_back_to_truncation_when_distiller_fails(monkeypatch: pytest.MonkeyPatch):
    distiller = _FakeDistiller(None)  # distillation failed/empty
    fetcher = _fetcher_with_body(monkeypatch, _BIG_BODY, distiller=distiller)
    page = await fetcher.fetch(OWNER, "http://93.184.216.34/", goal="x")
    assert distiller.calls == 1
    assert "Fetched content truncated" in page.content  # fell back to the truncation path


async def test_distiller_windows_and_drops_irrelevant():
    def _respond(messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        contents = (
            cast(object, getattr(part, "content", None)) for m in messages for part in m.parts
        )
        text = " ".join(content for content in contents if isinstance(content, str))
        if "B" * 12 in text:
            out = "NO RELEVANT CONTENT"  # middle window is irrelevant
        elif "A" * 12 in text:
            out = "KEPT-A"
        else:
            out = "KEPT-C"
        return ModelResponse(parts=[TextPart(content=out)])

    async def _resolve() -> tuple[Model, ModelSettings | None]:
        return FunctionModel(_respond), None

    body = "A" * 12 + "B" * 12 + "C" * 12  # 3 windows at window_tokens=3 (12 chars each)
    distiller = WebDistiller(
        resolve_model=_resolve, instructions="x", window_tokens=3, max_windows=8, timeout_s=30
    )
    out = await distiller.distill(body, goal="g", url="http://x/")
    assert out == "KEPT-A\n\nKEPT-C"  # the middle NO RELEVANT CONTENT window is dropped

    # A coverage note appears when max_windows caps below the window count.
    capped = WebDistiller(
        resolve_model=_resolve, instructions="x", window_tokens=3, max_windows=2, timeout_s=30
    )
    out2 = await capped.distill(body, goal="g", url="http://x/")
    assert out2 is not None
    assert "covered the first 24 of 36 characters" in out2


# --- adaptive settle loop (no browser) -------------------------------------


@final
class _SettlePage(SnapshotPage):
    """A page whose successive innerText snapshots follow ``texts`` (last value repeats).
    ``waits`` counts settle-loop waits — render_wait_ms is 0 in these tests, so a wait can
    only come from the settle loop."""

    def __init__(self, texts: list[str]):
        self._texts: list[str] = texts
        self._i: int = 0
        self.waits: int = 0

    @property
    @override
    def url(self) -> str:
        return "http://93.184.216.34/"

    @override
    async def wait_for_timeout(self, timeout: float) -> None:
        self.waits += 1

    @override
    async def content(self) -> str:
        return "<html><body></body></html>"

    @override
    async def evaluate(self, expression: str) -> str:
        text = self._texts[min(self._i, len(self._texts) - 1)]
        self._i += 1
        return text


async def test_capture_resnapshots_thin_page_until_content_appears():
    fetcher = BrowserFetcher(browser=_AvailableBrowser(), render_wait_ms=0)
    page = _SettlePage(["thin shell", "RICHBODY " * 6000])  # first snapshot thin, then ~54k
    _final_url, _html, text = await fetcher.capture("http://93.184.216.34/", page)
    assert "RICHBODY" in text  # settled onto the filled-in content
    assert page.waits > 0  # the settle loop actually waited


async def test_capture_fast_path_skips_settle_waits():
    fetcher = BrowserFetcher(browser=_AvailableBrowser(), render_wait_ms=0)
    page = _SettlePage(["RICHBODY " * 6000])  # already rich and stable on the first snapshot
    _final_url, _html, text = await fetcher.capture("http://93.184.216.34/", page)
    assert "RICHBODY" in text
    assert page.waits == 0  # a rich, stable first snapshot pays no extra settle waits


# --- PDF fetch path (no browser) -------------------------------------------


def _minimal_pdf(text: str = "Hello PDF World") -> bytes:
    """A hand-written one-page PDF with an extractable text layer (pdfium rebuilds the
    xref, so the trailer alone is enough)."""
    stream = f"BT /F1 24 Tf 72 700 Td ({text}) Tj ET".encode()
    return (
        b"%PDF-1.4\n"
        + b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
        + b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n"
        + b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        + b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>\nendobj\n"
        + b"4 0 obj\n<< /Length "
        + str(len(stream)).encode()
        + b" >>\nstream\n"
        + stream
        + b"\nendstream\nendobj\n"
        + b"5 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\nendobj\n"
        + b"trailer\n<< /Root 1 0 R >>\n%%EOF\n"
    )


@final
class _PdfDownloadBrowser(RenderBrowser, RenderContext, RenderPage):
    """available; its page's goto raises the 'Download is starting' PlaywrightError that a
    `.pdf` URL provokes — the fetcher must route to the direct-download PDF path."""

    @property
    @override
    def available(self) -> bool:
        return True

    @override
    @asynccontextmanager
    async def context(self, url: str | None = None) -> AsyncGenerator[_PdfDownloadBrowser]:
        yield self

    @override
    async def new_page(self) -> _PdfDownloadBrowser:
        return self

    @override
    async def apply_stealth(self, page: RenderPage) -> None:
        return None

    @property
    @override
    def url(self) -> str:
        raise AssertionError("unexpected call: url")

    @override
    async def wait_for_timeout(self, timeout: float) -> None:
        raise AssertionError("unexpected call: wait_for_timeout")

    @override
    async def content(self) -> str:
        raise AssertionError("unexpected call: content")

    @override
    async def evaluate(self, expression: str) -> str:
        raise AssertionError("unexpected call: evaluate")

    @override
    async def goto(
        self, url: str, *, wait_until: WaitUntil, timeout: float
    ) -> RenderResponse | None:
        from playwright.async_api import Error as PlaywrightError

        raise PlaywrightError("Page.goto: net::ERR_ABORTED; Download is starting")


async def test_download_error_routes_to_pdf_path(monkeypatch: pytest.MonkeyPatch):
    async def _fake_pdf(_url: str, **_kwargs: object) -> str:
        return "Attention Is All You Need — full paper text"

    monkeypatch.setattr("services.webfetch.fetcher.fetch_pdf_text", _fake_pdf)
    fetcher = BrowserFetcher(browser=_PdfDownloadBrowser())
    page = await fetcher.fetch(OWNER, "http://93.184.216.34/paper.pdf")
    assert "Attention Is All You Need" in page.content
    assert '<untrusted id="' in page.content


async def test_fetch_pdf_text_follows_redirect_recheck(monkeypatch: pytest.MonkeyPatch):
    from services.webfetch import pdf as pdf_mod

    checked: list[str] = []

    async def _record(url: str) -> None:
        checked.append(url)

    monkeypatch.setattr(pdf_mod, "assert_public_url", _record)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/redir":
            return httpx.Response(302, headers={"location": "https://cdn.example/final.pdf"})
        return httpx.Response(
            200, headers={"content-type": "application/pdf"}, content=_minimal_pdf()
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)
    try:
        text = await pdf_mod.fetch_pdf_text(
            "https://start.example/redir",
            timeout_s=5,
            max_bytes=10_000_000,
            max_pages=10,
            client=client,
        )
    finally:
        await client.aclose()
    assert "Hello PDF World" in text
    assert len(checked) >= 2  # re-checked SSRF before the redirect target too


async def test_fetch_pdf_text_refuses_non_pdf_download(monkeypatch: pytest.MonkeyPatch):
    from services.webfetch import pdf as pdf_mod

    monkeypatch.setattr(pdf_mod, "assert_public_url", _allow_all)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "text/html"}, content=b"<html>nope</html>"
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(WebFetchError):
            _ = await pdf_mod.fetch_pdf_text(
                "https://x.example/f",
                timeout_s=5,
                max_bytes=10_000_000,
                max_pages=10,
                client=client,
            )
    finally:
        await client.aclose()


async def test_fetch_pdf_text_scanned_pdf_raises(monkeypatch: pytest.MonkeyPatch):
    from services.webfetch import pdf as pdf_mod

    monkeypatch.setattr(pdf_mod, "assert_public_url", _allow_all)
    # A valid PDF header but no text layer at all (no content stream) → scanned.
    scanned = (
        b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
        + b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n"
        + b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] >>\nendobj\n"
        + b"trailer\n<< /Root 1 0 R >>\n%%EOF\n"
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "application/pdf"}, content=scanned)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(WebFetchError, match="no extractable text layer"):
            _ = await pdf_mod.fetch_pdf_text(
                "https://x.example/scan.pdf",
                timeout_s=5,
                max_bytes=10_000_000,
                max_pages=10,
                client=client,
            )
    finally:
        await client.aclose()


async def test_fetch_pdf_text_size_cap(monkeypatch: pytest.MonkeyPatch):
    from services.webfetch import pdf as pdf_mod

    monkeypatch.setattr(pdf_mod, "assert_public_url", _allow_all)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/pdf", "content-length": "99999999"},
            content=b"%PDF-",
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(WebFetchError, match="exceeds"):
            _ = await pdf_mod.fetch_pdf_text(
                "https://x.example/big.pdf",
                timeout_s=5,
                max_bytes=1000,
                max_pages=10,
                client=client,
            )
    finally:
        await client.aclose()


# --- VM-hosted browser: a real render (skipped without a real VM) ------------


async def _browser_or_skip(tmp_path: Path) -> tuple[ManagedBrowser, PlatformVM]:
    """A browser in a real platform VM; set ``HEPHAESTUS_TEST_VM=1`` on a host that has the
    VM backend and its image to run it."""
    if not os.environ.get("HEPHAESTUS_TEST_VM"):
        pytest.skip("set HEPHAESTUS_TEST_VM=1 to render in a real platform VM")
    settings = Settings(data_dir=tmp_path)
    vm = PlatformVM(
        SmolvmDriver(
            state_dir=tmp_path / "vm",
            cpus=settings.vm_cpus,
            memory_mb=settings.vm_memory_mb,
            disk_gb=settings.vm_disk_gb,
            boot_timeout_s=settings.vm_boot_timeout_s,
            provision_timeout_s=settings.vm_provision_timeout_s,
            image=Path(settings.vm_image) if settings.vm_image else None,
            ca_bundle=Path(settings.vm_ca_bundle) if settings.vm_ca_bundle else None,
        ),
        scratch=tmp_path / "vm" / "transfer",
    )
    await vm.start()
    if not await vm.wait_ready(settings.vm_boot_timeout_s + settings.vm_provision_timeout_s):
        await vm.stop()
        pytest.fail(f"the platform VM did not come up: {vm.failure}")
    browser = ManagedBrowser(
        enabled=True,
        vm=vm,
        startup_timeout_s=45.0,
        concurrency=2,
        user_agent="",
        locale="en-US",
        timezone_id="America/New_York",
    )
    await browser.start()
    if not await browser.wait_available(600.0):
        await browser.stop()
        await vm.stop()
        pytest.fail("the browser did not connect in the platform VM")
    return browser, vm


async def _allow_all(_url: str) -> None:
    return None


async def _js(page: Page, expression: str) -> object:
    return cast(object, await page.evaluate(expression))


async def test_vm_browser_renders_js_extracts_and_is_stealthed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    browser, vm = await _browser_or_skip(tmp_path)
    try:
        # 1) The fetch pipeline renders a JS-built page and extracts its content as Markdown.
        # Let the synthetic data: URL past the entry pre-flight (the guard treats data: as
        # inert, so no real SSRF policy is bypassed for network egress).
        monkeypatch.setattr("services.webfetch.fetcher.assert_public_url", _allow_all)
        fetcher = BrowserFetcher(browser=browser, min_chars=50, render_wait_ms=100)
        page_html = (
            "<html><head><title>Dyn</title></head><body><div id='r'></div>"
            "<script>document.getElementById('r').innerHTML="
            "'<article><h1>Dynamic Heading</h1><p>'"
            "+'real rendered content here. '.repeat(20)+'</p></article>'</script>"
            "</body></html>"
        )
        page = await fetcher.fetch(OWNER, "data:text/html," + urllib.parse.quote(page_html))
        # The content only exists after the script runs — proves we render, not just GET.
        assert "Dynamic Heading" in page.content
        assert "real rendered content here" in page.content
        assert '<untrusted id="' in page.content

        # 2) The browser presents as a normal user's Chrome, not an automated headless one.
        async with browser.context() as ctx:
            probe = await ctx.new_page()
            await browser.apply_stealth(probe)  # client-hint/UA-metadata override
            _ = await probe.goto("data:text/html,<p>ok</p>", wait_until="domcontentloaded")
            assert await _js(probe, "() => navigator.webdriver") in (False, None)
            user_agent = await _js(probe, "() => navigator.userAgent")
            assert isinstance(user_agent, str)
            assert "HeadlessChrome" not in user_agent and "Chrome/" in user_agent
            assert await _js(probe, "() => typeof window.chrome") == "object"
            languages = await _js(probe, "() => navigator.languages.length")
            assert isinstance(languages, int) and languages > 0
            # A fuller window.chrome (app/csi), not the bare { runtime } stub a probe spots.
            assert await _js(probe, "() => typeof window.chrome.app") == "object"
            assert await _js(probe, "() => typeof window.chrome.csi") == "function"
            # Named plugin entries — the old [1,2,3,4,5] left plugins[0].name undefined.
            assert await _js(probe, "() => navigator.plugins.length") == 5
            assert await _js(probe, "() => navigator.plugins[0].name") == "PDF Viewer"
            # The WebGL renderer agrees with the Linux UA — the old spoof leaked a macOS
            # 'Intel Iris' string under a Linux UA, a cross-check a fingerprinter flags.
            renderer = await _js(
                probe,
                "() => { const c = document.createElement('canvas').getContext('webgl');"
                + " return c ? c.getParameter(37446) : ''; }",
            )
            assert isinstance(renderer, str)
            assert "Iris" not in renderer and ("Mesa" in renderer or "ANGLE" in renderer)

        # 3) Client hints agree with the UA (the tell a patched UA string alone misses):
        # the headless shell's 'HeadlessChrome' brand must be gone from navigator.userAgentData
        # and the Sec-CH-UA it derives. userAgentData needs a secure context, which a
        # route-fulfilled https URL provides offline — no network egress.
        async with browser.context() as ctx:

            async def _ok(route: Route) -> None:
                await route.fulfill(status=200, content_type="text/html", body="<p>ok</p>")

            _ = await ctx.route("https://stealth.local/**", _ok)
            probe = await ctx.new_page()
            await browser.apply_stealth(probe)
            _ = await probe.goto("https://stealth.local/", wait_until="domcontentloaded")
            assert await _js(probe, "() => isSecureContext") is True
            brands = cast(
                list[str],
                await probe.evaluate("() => navigator.userAgentData.brands.map(b => b.brand)"),
            )
            assert brands and not any("HeadlessChrome" in b for b in brands)
            assert any("Chrome" in b for b in brands)
            assert await _js(probe, "() => navigator.userAgentData.platform") == "Linux"
    finally:
        await browser.stop()
        await vm.stop()


# --- agent reaches the fetch tool through the toolset stack ----------------


async def test_web_fetch_tool_reaches_the_fetcher():
    from pydantic_ai.models.test import TestModel

    from agent import build_chat_orchestrator
    from core.container import ServiceContainer
    from runs import RunRegistry, RunStatus
    from services.webfetch import BrowserFetcher
    from tools.search import web_toolset

    seen: dict[str, str] = {}

    class _StubFetcher:
        async def fetch(self, _owner_id: str, url: str, **_kwargs: object) -> FetchedPage:
            seen["url"] = url
            return FetchedPage(url=url, title="t", content="body")

    caps = ServiceContainer()
    caps.add(_StubFetcher(), as_type=BrowserFetcher)
    orch = build_chat_orchestrator(
        "read it",
        model=TestModel(call_tools=["web_fetch"]),
        categories={"web": web_toolset()},
        capabilities=caps,
    )
    run = RunRegistry().submit(kind="chat", owner_id=OWNER, orchestrator=orch)
    _ = await run.wait()

    assert run.status is RunStatus.done
    assert "url" in seen, "the fetch tool should have reached the fetcher capability"
    citations = [e.body for e in run.stream.replay() if e.body.type == "citation.added"]
    assert len(citations) == 1
    assert citations[0].url == seen["url"]
    assert citations[0].title == "t"
