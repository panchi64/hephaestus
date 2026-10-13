"""Finding a Chromium's DevTools endpoint — shared by both browsers this app runs.

Two very different browsers need the same three lines: the headless one in the platform VM that web
fetch renders pages in, and the visible one on the host the agent drives. Both are reached
by asking ``/json/version`` for its websocket URL and then **rewriting the authority**,
because the endpoint a browser reports is the one it sees itself on — the guest's own
loopback port, or a hostname that resolves to nothing out here. The port we can actually
reach it on is the one we published or asked for, so that is the one that goes in the URL.

Polling rather than a single read: a port accepts connections before Chromium has finished
bringing DevTools up, so the first few reads legitimately fail and only the deadline is a
real answer.
"""

from __future__ import annotations

import asyncio
import json
import urllib.parse

import httpx

_POLL_INTERVAL_S = 0.25


async def discover_cdp_ws(host_port: int, timeout_s: float) -> str | None:
    """The CDP websocket endpoint reachable on ``127.0.0.1:host_port``, or ``None`` when
    it never appears within ``timeout_s``. Callers treat ``None`` as "no browser" and
    degrade — it is never an exception, because every caller here is already on a
    best-effort bring-up path."""
    async with httpx.AsyncClient() as client:
        for _ in range(int(timeout_s / _POLL_INTERVAL_S) + 1):
            try:
                resp = await client.get(f"http://127.0.0.1:{host_port}/json/version", timeout=2.0)
                if resp.status_code == 200:
                    raw = json.loads(resp.text)["webSocketDebuggerUrl"]
                    parts = urllib.parse.urlsplit(raw)
                    return urllib.parse.urlunsplit(
                        (parts.scheme, f"127.0.0.1:{host_port}", parts.path, "", "")
                    )
            except httpx.HTTPError, KeyError, ValueError:
                pass
            await asyncio.sleep(_POLL_INTERVAL_S)
    return None
