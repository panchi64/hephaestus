"""Fetch a remote image on the operator's behalf, so their browser never does.

An answer can hyperlink the web (see ``prompts.agent``), and an image is the natural
next step — a chart on the page the model just read is worth showing, not describing.
But ``<img src="https://…">`` is not like a link: it fetches **on render**, with no
click to gate it. Pointed at a host chosen by relayed, untrusted page content, that is
a silent request carrying the operator's IP, user-agent, and any cookies for that
host — the classic markdown-image exfiltration channel.

So the browser never sees the remote URL. It asks *us* (``routes.media``), and this
service does the fetching, one origin removed from the operator:

* :func:`core.ssrf.assert_public_url` on the URL and again on every redirect hop, so
  the proxy can't be aimed at loopback, the LAN, or a cloud metadata endpoint.
* The response has to actually be a raster image — declared content type **and**
  magic bytes must agree on a format in :data:`IMAGE_TYPES`. A server that answers an
  ``<img>`` with HTML is not serving an image, and we don't pass it on.
* A byte cap enforced *while streaming*, so a hostile or mistaken URL can't stream
  gigabytes through the backend before anyone notices the declared length was a lie.

What this deliberately does not solve: the remote host still learns its URL was
requested, so a URL that encodes exfiltrated data still arrives — from this machine's
address, at a time correlated with the answer. Closing that needs a human click before
the fetch, which is a product decision, not a service one. The agent's own
``web_fetch`` can already reach an arbitrary URL; what this removes is the operator's
identity and the silence.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from core.exceptions import HephaestusError
from core.ssrf import assert_public_url

# Formats worth rendering inline, each with the magic-byte prefix that proves it.
#
# SVG is absent on purpose. It is XML, it can carry script, and it is the one image
# format whose bytes become a *document* if the response is ever opened directly
# rather than through an `<img>`. The headers this is served with make that inert, but
# a raster allowlist means we are not relying on them.
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
    """Validated image bytes and the content type they were *sniffed* as — never the
    type the remote server claimed, so a mislabelled response can't pick the type the
    browser then trusts."""

    content_type: str
    data: bytes


def _sniff(data: bytes) -> str | None:
    """The image type ``data`` actually is, by its magic bytes, or ``None``."""
    for content_type, prefixes in IMAGE_TYPES:
        for prefix in prefixes:
            if content_type == "image/webp":
                # RIFF<u32 size>WEBP — the length between the two markers is the
                # file's, so match both ends rather than a single prefix.
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

    Redirects are followed by hand rather than by the client, because each hop's target
    has to clear :func:`assert_public_url` before it is contacted — a public host that
    redirects to ``127.0.0.1`` is the open-redirect case the guard exists for, and a
    client following redirects internally would already have made that request.
    """
    seen = url
    for _ in range(_MAX_REDIRECTS + 1):
        await assert_public_url(seen)
        try:
            # `follow_redirects=False`: see above. No operator credentials are on this
            # client, and `Accept` states what we are willing to render.
            async with client.stream(
                "GET",
                seen,
                timeout=timeout_s,
                follow_redirects=False,
                headers={"Accept": "image/*"},
            ) as resp:
                if resp.is_redirect:
                    location = resp.headers.get("location")
                    if not location:
                        raise ImageFetchError("redirect without a destination")
                    seen = str(httpx.URL(seen).join(location))
                    continue
                if resp.status_code >= 400:
                    raise ImageFetchError(f"remote returned HTTP {resp.status_code}")
                declared = resp.headers.get("content-type", "").split(";")[0].strip().lower()
                if declared and not declared.startswith("image/"):
                    raise ImageFetchError(f"remote served {declared!r}, not an image")
                # Read to the cap +1 so an over-size body is *detected* rather than
                # silently truncated into a corrupt image. Streaming, so the declared
                # Content-Length (which a hostile server can lie about) is never trusted.
                chunks: list[bytes] = []
                total = 0
                async for chunk in resp.aiter_bytes():
                    total += len(chunk)
                    if total > max_bytes:
                        raise ImageFetchError(f"image exceeds {max_bytes} bytes")
                    chunks.append(chunk)
        except httpx.HTTPError as exc:
            raise ImageFetchError(f"could not be fetched: {exc}") from exc

        data = b"".join(chunks)
        sniffed = _sniff(data)
        if sniffed is None:
            raise ImageFetchError("bytes are not a supported image format")
        return FetchedImage(content_type=sniffed, data=data)

    raise ImageFetchError("too many redirects")
