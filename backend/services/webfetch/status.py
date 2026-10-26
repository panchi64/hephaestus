"""What to do next about a page that answered with an error status."""

from __future__ import annotations

import re

_MISSING = frozenset({404, 410})

# Cloudflare's own error pages for a hostname it no longer routes (DNS resolution error,
# unknown host, origin DNS failure, …). Firewall and rate-limit codes (1010, 1015, 1020) are
# deliberately absent: those hosts are alive and the page may exist.
_CLOUDFLARE_DEAD_HOST = re.compile(r"error code:\s*10(?:00|01|02|14|16|18)\b", re.IGNORECASE)


def status_hint(status: int, body: str = "") -> str | None:
    """The next step for a fetch that came back with ``status`` and error ``body``, or None
    for no advice.

    A missing page or a dead host is most often an address the model reconstructed from
    memory, which a near variant will not fix.
    """
    if status in _MISSING or _CLOUDFLARE_DEAD_HOST.search(body[:2000]):
        return (
            "there is no page at that address; do not guess addresses or try variants of "
            "it — run `web_search` and fetch a URL it returned"
        )
    return None
