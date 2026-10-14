"""``core.net.await_http_ready`` — the shared "it is actually serving" probe.

A *bound* port is not a serving one: a dev server or an inference engine accepts
connections well before it answers a request. Both the sandbox preview and the engine
supervisor wait on this, and they disagree only about what a timeout means, so the
contract they share is what these cover.
"""

from __future__ import annotations

import asyncio
import socket
from asyncio.trsock import TransportSocket
from typing import cast

import pytest

from core.net import await_http_ready


def _port(sock: socket.socket | TransportSocket) -> int:
    return cast(tuple[str, int], sock.getsockname())[1]


async def _serving(responses: list[bytes]) -> tuple[asyncio.Server, int]:
    """A loopback server that answers each connection with the next canned response."""
    remaining = list(responses)

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        _ = await reader.read(4096)
        writer.write(remaining.pop(0) if remaining else responses[-1])
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    return server, _port(server.sockets[0])


_OK = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok"
_BOOTING = b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
_NO_ROUTE = b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"


async def test_a_5xx_is_still_warming_up_and_gets_polled_through():
    server, port = await _serving([_BOOTING, _BOOTING, _OK])
    async with server:
        ready = await asyncio.wait_for(
            await_http_ready(f"http://127.0.0.1:{port}/", 5.0, poll_interval_s=0.01),
            timeout=5.0,
        )
    assert ready is True


async def test_a_404_counts_as_serving():
    # The probe hits a path the server may have no route for — being answered at all is
    # the fact we want, and treating a 404 as not-ready would hang on every static server.
    server, port = await _serving([_NO_ROUTE])
    async with server:
        assert await asyncio.wait_for(
            await_http_ready(f"http://127.0.0.1:{port}/", 5.0), timeout=5.0
        )


async def test_running_out_of_budget_returns_false_rather_than_raising():
    # The two callers read a timeout differently — a failed engine start vs a preview
    # worth opening anyway — so the probe reports rather than decides.
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = _port(sock)
    sock.close()

    result = await asyncio.wait_for(
        await_http_ready(f"http://127.0.0.1:{port}/", 0.3, poll_interval_s=0.05), timeout=3.0
    )
    assert result is False


async def test_a_process_that_already_exited_fails_fast_instead_of_waiting_out_the_budget():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = _port(sock)
    sock.close()

    loop = asyncio.get_running_loop()
    started = loop.time()
    with pytest.raises(ConnectionError):
        _ = await await_http_ready(f"http://127.0.0.1:{port}/", 30.0, is_alive=lambda: False)
    assert loop.time() - started < 5.0  # nowhere near the 30s budget
