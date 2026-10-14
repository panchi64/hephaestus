"""Rendered DOM → Markdown extraction.

The fetcher renders a page in a headless browser and hands the fully-rendered HTML
here. **trafilatura** is the primary extractor — the top-scoring open-source main-content
extractor — run over the HTML string (independent of its own downloader) and emitting
Markdown natively, preserving tables/links/code. It is itself a cascade (its own
heuristics → readability-lxml → jusText). When it returns nothing usable we fall back to
the page's rendered ``innerText`` — a genuinely different failure mode that covers
non-prose pages (search results, forums, listings) and guarantees non-empty output when
visible text exists.

The extractor list is a **seam**: add a tier (a dedicated HTML→Markdown converter, or a
scored ensemble) by appending to :data:`EXTRACTORS` — the cascade and the innerText
fallback stay the same.
"""

from __future__ import annotations

from collections.abc import Callable

import trafilatura
from lxml.html import HtmlElement

# An extractor turns the parsed tree + the page URL into Markdown (or None).
Extractor = Callable[[HtmlElement, str], str | None]


def _trafilatura(tree: HtmlElement, url: str) -> str | None:
    # favor_recall keeps more of the page (we render fully, so there is real content to
    # keep); tables/links/formatting preserve structure for technical/doc pages.
    return trafilatura.extract(
        tree,
        url=url,
        output_format="markdown",
        include_tables=True,
        include_links=True,
        include_formatting=True,
        favor_recall=True,
        with_metadata=False,
    )


# Ordered best-first. The first output that clears ``min_chars`` wins; otherwise the
# longest non-empty candidate (every extractor's output + the rendered innerText) is used.
EXTRACTORS: list[Extractor] = [_trafilatura]


def extract(
    html: str,
    *,
    url: str,
    rendered_text: str,
    min_chars: int,
    innertext_ratio: float = 0.25,
    innertext_floor: int = 2000,
) -> tuple[str | None, str | None]:
    """Return ``(title, content)`` for a rendered page. ``content`` is Markdown (or the
    rendered innerText fallback), or ``None`` when nothing readable was found — the
    caller raises ``WebFetchError`` in that case.

    An extractor output that clears ``min_chars`` normally wins outright. But when the
    extraction is itself **thin** (``< innertext_floor``) while the page carries a lot of
    visible text (``>= innertext_floor``) and the extraction is only a small fraction
    (``< innertext_ratio``) of that rendered innerText, that output is **demoted to a
    candidate** so innerText can compete by length — the main-content heuristic
    under-selected (a homepage or JS-heavy page whose real body it missed). The
    ``< innertext_floor`` clause matters: a substantial clean article (well over the floor)
    sitting on a comment/nav-heavy page is a small *fraction* of the innerText yet is
    exactly the content we want, so it must not be demoted in favour of raw noise."""
    tree = trafilatura.load_html(html)
    title = _title(tree)
    rendered = (rendered_text or "").strip()
    candidates: list[str] = []
    if tree is not None:
        for extractor in EXTRACTORS:
            try:
                out = extractor(tree, url)
            except Exception:
                out = None
            stripped = out.strip() if out else ""
            if stripped:
                thin_fraction = (
                    len(stripped) < innertext_floor
                    and len(rendered) >= innertext_floor
                    and len(stripped) < innertext_ratio * len(rendered)
                )
                if len(stripped) >= min_chars and not thin_fraction:
                    return title, stripped
                candidates.append(stripped)
    if rendered:
        candidates.append(rendered)
    return title, (max(candidates, key=len) if candidates else None)


def _title(tree: HtmlElement | None) -> str | None:
    if tree is None:
        return None
    try:
        meta = trafilatura.extract_metadata(tree)
    except Exception:
        return None
    return meta.title
