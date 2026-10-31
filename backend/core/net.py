"""HTTP response helpers, and loopback readiness probes for the servers we spawn.

No domain coupling, so each caller wraps the outcome in its own error (``SandboxError``, …).

Readiness has two levels, because *bound* and *serving* are different facts: a dev server
accepts connections well before it answers a request, and a client that takes the bind as
the go-ahead gets a connection reset for its first fetch.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Callable
from contextlib import suppress

import httpx


class ResponseTooLarge(Exception):
    """A streamed response body crossed its byte cap."""


def header(headers: httpx.Headers, name: str) -> str | None:
    """The header's value, repeated occurrences joined with commas, or ``None`` when absent."""
    values = headers.get_list(name)
    return ", ".join(values) if values else None


async def iter_capped(response: httpx.Response, max_bytes: int) -> AsyncGenerator[bytes]:
    """The body's chunks, raising :class:`ResponseTooLarge` once they pass ``max_bytes``.

    Counted as it streams, so a missing or lying ``Content-Length`` can't delay the cap.
    """
    total = 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > max_bytes:
            raise ResponseTooLarge(f"the response exceeds {max_bytes} bytes")
        yield chunk


async def await_listening(
    port: int,
    timeout_s: float,
    *,
    host: str = "127.0.0.1",
    poll_interval_s: float = 0.25,
    is_alive: Callable[[], bool] | None = None,
) -> None:
    """Poll ``host:port`` until a TCP connection succeeds.

    Raises ``TimeoutError`` if nothing listens within ``timeout_s``, and ``ConnectionError``
    as soon as ``is_alive`` returns ``False``.
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

    A 5xx or a connection error means still warming up. A 404 counts as serving, since the
    probe usually hits a path with no route.

    Running out of budget returns ``False`` rather than raising, because callers disagree
    on what a timeout means. ``is_alive`` behaves as in :func:`await_listening`. Each
    request and sleep is clamped to the remaining budget, so a hung probe can't overshoot.
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
