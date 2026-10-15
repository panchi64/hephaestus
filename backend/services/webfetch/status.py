"""What to do next about a page that answered with an error status."""

from __future__ import annotations

_MISSING = frozenset({404, 410})


def status_hint(status: int) -> str | None:
    """The next step for a fetch that came back with ``status``, or None for no advice.

    A missing page is most often an address the model reconstructed from memory, which a
    near variant will not fix.
    """
    if status in _MISSING:
        return (
            "there is no page at that address; do not guess addresses or try variants of "
            "it — run `web_search` and fetch a URL it returned"
        )
    return None
