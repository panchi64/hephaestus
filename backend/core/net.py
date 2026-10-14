"""Loopback readiness — wait for a subprocess to bind its port, and to start serving on it.

The shared probes behind every server we spawn and wait on: preview dev servers, the
managed SearXNG and the web-fetch browser. No domain coupling,
so each caller wraps the outcome in its own error (``SandboxError``, …).

Two levels, because *bound* and *serving* are different facts: a dev server or an
inference engine accepts connections well before it answers a request, and a client that
takes the bind as the go-ahead gets a connection reset for its first fetch.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import suppress

import httpx


def header(headers: httpx.Headers, name: str) -> str | None:
    """The header's value, repeated occurrences joined with commas, or ``None`` when absent."""
    values = headers.get_list(name)
    return ", ".join(values) if values else None


async def await_listening(
    port: int,
    timeout_s: float,
    *,
    host: str = "127.0.0.1",
    poll_interval_s: float = 0.25,
    is_alive: Callable[[], bool] | None = None,
) -> None:
    """Poll ``host:port`` until a TCP connection succeeds.

    Raises ``TimeoutError`` if nothing is listening within ``timeout_s``. When
    ``is_alive`` is supplied and returns ``False`` (the process we're waiting on has
    already exited), raise ``ConnectionError`` immediately instead of waiting out the
    full timeout — so a server that dies on startup fails fast with a clear cause.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while True:
        if is_alive is not None and not is_alive():
            raise ConnectionError("the process exited before it started listening")
        try:
            _reader, writer = await asyncio.open_connection(host, port)
            writer.close()
            with suppress(Exception):
                await writer.wait_closed()
            return
        except OSError, ConnectionError:
            if loop.time() >= deadline:
                raise TimeoutError(
                    f"{host}:{port} did not start listening within {timeout_s:.0f}s"
                ) from None
            await asyncio.sleep(poll_interval_s)


async def await_http_ready(
    url: str,
    timeout_s: float,
    *,
    poll_interval_s: float = 0.25,
    request_timeout_s: float = 2.0,
    is_alive: Callable[[], bool] | None = None,
) -> bool:
    """Poll ``url`` until it answers with a non-5xx status. Returns whether it did.

    A 5xx (or a connection error) is "still warming up"; anything below 5xx means the
    server is serving, including a 404 — the probe usually hits a path the server has no
    route for, and being answered at all is the fact we want.

    Returns ``False`` on running out of budget rather than raising, because the two
    callers disagree about what a timeout means: an engine that never became ready is a
    failed start, while a sandbox preview that hasn't answered yet is still worth opening
    (the operator's refresh button is the backstop). Neither reading belongs here.

    ``is_alive`` behaves as in :func:`await_listening` — when the process being waited on
    has already exited, raise ``ConnectionError`` at once instead of polling a port that
    will never answer. Each request and each sleep is clamped to the remaining budget, so
    the call never overshoots ``timeout_s`` even when a probe hangs.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    async with httpx.AsyncClient() as client:
        while True:
            if is_alive is not None and not is_alive():
                raise ConnectionError("the process exited before it started serving")
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            try:
                resp = await client.get(url, timeout=min(remaining, request_timeout_s))
                if resp.status_code < 500:
                    return True
            except httpx.HTTPError:
                pass  # not answering yet — keep polling within the budget
            await asyncio.sleep(min(poll_interval_s, max(deadline - loop.time(), 0.0)))
