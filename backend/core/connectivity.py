"""Internet reachability — is the host actually online?

A counterpart to :mod:`core.net` (which waits on a *loopback* server to bind): this
asks the opposite question about the *public* internet. The signal is a raw TCP connect
to one of a few well-known anchor IPs on :443 — direct addresses (no DNS), no payload,
no HTTP, no phone-home to a tracker. The first anchor that accepts a connection means
"online"; all failing within the timeout means "offline".

Used by the offline-mode monitor (:mod:`services.offline`) to decide whether the managed
web services are worth running. Pure stdlib asyncio, no domain coupling.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress

logger = logging.getLogger(__name__)


def _split_anchor(anchor: str, default_port: int = 443) -> tuple[str, int]:
    """``"1.1.1.1:443"`` → ``("1.1.1.1", 443)``; a bare host uses ``default_port``."""
    host, sep, port = anchor.rpartition(":")
    if not sep:  # no ':' — the whole string is the host
        return anchor, default_port
    try:
        return host, int(port)
    except ValueError:  # a non-numeric port — keep the parsed host, fall back on the port
        return host, default_port


async def _can_connect(host: str, port: int, timeout_s: float) -> bool:
    try:
        _reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout_s
        )
    except OSError, TimeoutError:
        return False
    writer.close()
    with suppress(Exception):
        await writer.wait_closed()
    return True


async def check_online(anchors: list[str], timeout_s: float) -> bool:
    """``True`` as soon as any anchor accepts a TCP connection, else ``False``.

    Anchors are probed concurrently and the first success wins (the rest are
    cancelled), so a single reachable host returns fast without waiting out the
    unreachable ones. An empty anchor list conservatively reports offline.
    """
    if not anchors:
        logger.warning("connectivity: no anchors configured — reporting offline")
        return False
    tasks = [asyncio.ensure_future(_can_connect(*_split_anchor(a), timeout_s)) for a in anchors]
    try:
        online = False
        pending = set(tasks)
        while pending:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            if any(t.result() for t in done):
                online = True
                break
        return online
    finally:
        for t in tasks:
            _ = t.cancel()
        with suppress(Exception):
            _ = await asyncio.gather(*tasks, return_exceptions=True)
