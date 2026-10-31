"""Fetch a PDF a page navigation refused to render and pull its text layer.

Headless Chromium turns a `.pdf` URL into a download, so the bytes are fetched directly and
read with the upload pipeline's ``pypdfium2`` scanner. There is no OCR: a PDF with no text
layer is a recoverable ``WebFetchError``. Only domain errors leave this module.
"""

from __future__ import annotations

import asyncio

import httpx

from core.exceptions import WebFetchError
from core.net import ResponseTooLarge, header, iter_capped
from core.ssrf import assert_public_url
from services.upload_extraction import scan_pdf

from .status import ERROR_BODY_BYTES, served_by_cloudflare, status_hint

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
    """Fetch ``url``'s bytes, following redirects by hand so SSRF is re-checked on each hop."""
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
                    cloudflare_body = (
                        await _error_head(resp) if served_by_cloudflare(resp.headers) else ""
                    )
                    raise WebFetchError(
                        f"{url!r} returned HTTP {resp.status_code}",
                        hint=status_hint(resp.status_code, cloudflare_body=cloudflare_body),
                    )
                declared = header(resp.headers, "content-length")
                if declared is not None and declared.isdigit() and int(declared) > max_bytes:
                    raise WebFetchError(
                        f"the file at {url!r} exceeds the {max_bytes}-byte fetch limit"
                    )
                body = b"".join([chunk async for chunk in iter_capped(resp, max_bytes)])
                return body, resp.headers
        except ResponseTooLarge:
            raise WebFetchError(
                f"the file at {url!r} exceeds the {max_bytes}-byte fetch limit"
            ) from None
        except httpx.HTTPError as exc:
            raise WebFetchError(f"could not fetch {url!r}: {exc}") from exc
    raise WebFetchError(f"too many redirects fetching {url!r}")


async def _error_head(resp: httpx.Response) -> str:
    """The first chunk of an error page, capped at ``ERROR_BODY_BYTES``."""
    async for chunk in resp.aiter_bytes():
        return chunk[:ERROR_BODY_BYTES].decode(errors="replace")
    return ""
