"""What to do next about a page that answered with an error status."""

from __future__ import annotations

import re
from collections.abc import Mapping

_MISSING = frozenset({404, 410})

#: How much of a Cloudflare error page is read; its code sits near the top.
ERROR_BODY_BYTES = 8192

# Cloudflare codes for a hostname it no longer routes, in its plain-text and HTML error pages.
# Firewall and rate-limit codes (1010, 1015, 1020) are absent: those hosts are alive.
_CLOUDFLARE_DEAD_HOST = re.compile(
    r"\berror(?: code:)?(?:\s|<[^>]*>)*10(?:00|01|02|14|16|18)\b", re.IGNORECASE
)


def served_by_cloudflare(headers: Mapping[str, str]) -> bool:
    """Reads the ``Server`` header, which Cloudflare sets on every response it answers."""
    return "cloudflare" in headers.get("server", "").lower()


def status_hint(status: int, *, cloudflare_body: str = "") -> str | None:
    """The next step for an error ``status`` (and Cloudflare's error page, if it sent one)."""
    # A missing page or dead host is usually an address the model reconstructed from memory.
    if status in _MISSING or _CLOUDFLARE_DEAD_HOST.search(cloudflare_body):
        return (
            "there is no page at that address; do not guess addresses or try variants of "
            "it — run `web_search` and fetch a URL it returned"
        )
    return None
