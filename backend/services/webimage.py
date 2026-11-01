"""Fetch a remote image on the operator's behalf, so their browser never does.

An ``<img>`` fetches on render with no click to gate it, so a URL chosen by untrusted page
content would silently carry the operator's IP, user-agent and cookies to that host. The
browser asks ``routes.media`` instead, and this service fetches:

* :func:`core.ssrf.assert_public_url` runs on the URL and on every redirect hop.
* The declared content type and the magic bytes must agree on a format in
  :data:`IMAGE_TYPES`.
* A byte cap is enforced while streaming.

Not solved: the remote host still learns its URL was requested, so data encoded in the URL
still arrives. Closing that needs a human click before the fetch.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from core.exceptions import HephaestusError
from core.net import ResponseTooLarge, header, iter_capped
from core.ssrf import assert_public_url

# Formats worth rendering inline, each with the magic-byte prefix that proves it. SVG is
# excluded: it can carry script and becomes a document if opened outside an `<img>`.
IMAGE_TYPES: tuple[tuple[str, tuple[bytes, ...]], ...] = (
    ("image/png", (b"\x89PNG\r\n\x1a\n",)),
    ("image/jpeg", (b"\xff\xd8\xff",)),
    ("image/gif", (b"GIF87a", b"GIF89a")),
    # RIFF....WEBP — the size field sits between, so the check is split (see `_sniff`).
    ("image/webp", (b"RIFF",)),
    ("image/avif", (b"ftypavif", b"ftypavis")),
)

_MAX_REDIRECTS = 3


class ImageFetchError(HephaestusError):
    """The URL did not yield an image we are willing to render."""


@dataclass(frozen=True)
class FetchedImage:
    """Validated image bytes and the type they were sniffed as, never the type the server
    claimed, so a mislabelled response can't choose what the browser trusts."""

    content_type: str
    data: bytes


def _sniff(data: bytes) -> str | None:
    """The image type ``data`` actually is, by its magic bytes, or ``None``."""
    for content_type, prefixes in IMAGE_TYPES:
        for prefix in prefixes:
            if content_type == "image/webp":
                # RIFF<u32 size>WEBP: the size sits between the markers, so match both.
                if data.startswith(prefix) and data[8:12] == b"WEBP":
                    return content_type
            elif content_type == "image/avif":
                # The brand sits at offset 4, after the box length.
                if data[4:12].startswith(prefix):
                    return content_type
            elif data.startswith(prefix):
                return content_type
    return None


async def fetch_image(
    url: str,
    *,
    client: httpx.AsyncClient,
    timeout_s: float,
    max_bytes: int,
) -> FetchedImage:
    """Fetch ``url`` and return it only if it is a raster image within ``max_bytes``.

    Redirects are followed by hand so each hop's target clears :func:`assert_public_url`
    before it is contacted.
    """
    seen = url
    for _ in range(_MAX_REDIRECTS + 1):
        await assert_public_url(seen)
        try:
            async with client.stream(
                "GET",
                seen,
                timeout=timeout_s,
                follow_redirects=False,
                headers={"Accept": "image/*"},
            ) as resp:
                if resp.is_redirect:
                    location = header(resp.headers, "location")
                    if not location:
                        raise ImageFetchError("redirect without a destination")
                    seen = str(httpx.URL(seen).join(location))
                    continue
                if resp.status_code >= 400:
                    raise ImageFetchError(f"remote returned HTTP {resp.status_code}")
                declared = (
                    (header(resp.headers, "content-type") or "").split(";")[0].strip().lower()
                )
                if declared and not declared.startswith("image/"):
                    raise ImageFetchError(f"remote served {declared!r}, not an image")
                data = b"".join([chunk async for chunk in iter_capped(resp, max_bytes)])
        except ResponseTooLarge:
            raise ImageFetchError(f"image exceeds {max_bytes} bytes") from None
        except httpx.HTTPError as exc:
            raise ImageFetchError(f"could not be fetched: {exc}") from exc

        sniffed = _sniff(data)
        if sniffed is None:
            raise ImageFetchError("bytes are not a supported image format")
        return FetchedImage(content_type=sniffed, data=data)

    raise ImageFetchError("too many redirects")
