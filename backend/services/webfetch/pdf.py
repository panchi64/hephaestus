"""Fetch a PDF a page navigation refused to render and pull its text layer.

The headless browser can't *render* a PDF — a `.pdf` URL makes Chromium start a download,
which surfaces as a "Download is starting" navigation error. Rather than burn the fetch
tool's retry budget on an unreadable page, :func:`fetch_pdf_text` downloads the bytes
directly (SSRF-guarded on every redirect hop, size-capped) and extracts their text with the
same ``pypdfium2`` scanner the upload pipeline uses. There is no vision OCR here — a scanned
PDF with no text layer comes back as a recoverable ``WebFetchError`` telling the model to try
another source.

Domain errors only (``WebFetchError``/``SSRFError``), like the rest of ``webfetch`` — the
tool layer maps them to a retry vs a refusal.
"""

from __future__ import annotations

import asyncio

import httpx

from core.exceptions import WebFetchError
from core.net import header
from core.ssrf import assert_public_url
from services.upload_extraction import scan_pdf

from .status import status_hint

_MAX_REDIRECTS = 5


async def fetch_pdf_text(
    url: str,
    *,
    timeout_s: float,
    max_bytes: int,
    max_pages: int,
    client: httpx.AsyncClient | None = None,
) -> str:
    """Download the PDF at ``url`` and return its extracted text.

    Follows up to five redirect hops manually, re-checking SSRF before *every* hop (a
    public host can redirect to a private one). Refuses a download that is oversize or is
    not a PDF, and a PDF with no extractable text layer (scanned). ``client`` None ⇒ a
    transient client owned by this call."""
    owns = client is None
    client = client or httpx.AsyncClient()
    try:
        content, headers = await _download(url, client, timeout_s=timeout_s, max_bytes=max_bytes)
    finally:
        if owns:
            await client.aclose()

    is_pdf = content[:5] == b"%PDF-" or "pdf" in (header(headers, "content-type") or "").lower()
    if not is_pdf:
        raise WebFetchError(
            f"{url!r} served a download that is not a PDF; only PDF downloads can be read"
        )
    scan = await asyncio.to_thread(scan_pdf, content, max_pages)
    text = "\n\n".join(page.native_text for page in scan.pages).strip()
    if not text:
        raise WebFetchError(
            f"the PDF at {url!r} has no extractable text layer (it appears to be "
            + "scanned); try another source"
        )
    return text


async def _download(
    url: str, client: httpx.AsyncClient, *, timeout_s: float, max_bytes: int
) -> tuple[bytes, httpx.Headers]:
    """Fetch ``url``'s bytes, following redirects manually so SSRF is re-checked on each
    hop and the response is never streamed to a private address.

    The body is **streamed** and the running total checked against ``max_bytes`` as it
    arrives, so a response with no (or a lying) ``Content-Length`` can't materialize an
    unbounded body in memory before the cap is applied — the read stops the moment the
    limit is crossed."""
    current = url
    for _ in range(_MAX_REDIRECTS + 1):
        await assert_public_url(current)
        try:
            async with client.stream(
                "GET", current, timeout=timeout_s, follow_redirects=False
            ) as resp:
                if resp.is_redirect:
                    location = header(resp.headers, "location")
                    if not location:
                        raise WebFetchError(f"{current!r} returned a redirect with no location")
                    current = str(httpx.URL(current).join(location))
                    continue
                if resp.status_code >= 400:
                    raise WebFetchError(
                        f"{url!r} returned HTTP {resp.status_code}",
                        hint=status_hint(resp.status_code),
                    )
                declared = header(resp.headers, "content-length")
                if declared is not None and declared.isdigit() and int(declared) > max_bytes:
                    raise WebFetchError(
                        f"the file at {url!r} exceeds the {max_bytes}-byte fetch limit"
                    )
                chunks: list[bytes] = []
                total = 0
                async for chunk in resp.aiter_bytes():
                    total += len(chunk)
                    if total > max_bytes:
                        raise WebFetchError(
                            f"the file at {url!r} exceeds the {max_bytes}-byte fetch limit"
                        )
                    chunks.append(chunk)
                return b"".join(chunks), resp.headers
        except httpx.HTTPError as exc:
            raise WebFetchError(f"could not fetch {url!r}: {exc}") from exc
    raise WebFetchError(f"too many redirects fetching {url!r}")
