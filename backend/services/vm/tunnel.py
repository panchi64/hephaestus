"""Egress and previews over host-initiated connections only.

The VM has no network route of its own, so the guest cannot open a connection to
anything. Instead the host keeps a few idle connections open to the guest relay's pool
port (``vm/guest/relay.py``). When something in the guest needs the internet, the relay
hands it one of those, prefixed with a channel byte; the host side here reads the byte
and serves the rest through :class:`~services.vm.egress.EgressGateway`, then opens a
replacement. The guest can use a connection the host opened, but only to reach the
gateway.

Previews run the other way: a host loopback port per preview, each connection spliced
through the relay's mux to the workspace's preview socket.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import cast

from core.tasks import STOP_TIMEOUT_S, cancel_and_wait

from .egress import EgressGateway, splice

logger = logging.getLogger(__name__)

type Streams = tuple[asyncio.StreamReader, asyncio.StreamWriter]
type Connect = Callable[[], Awaitable[Streams]]

#: Idle connections held open at once — how many guest requests can start without
#: waiting for the host to open another.
IDLE_CONNECTIONS = 8
_RETRY_S = (0.2, 0.5, 1.0, 2.0, 5.0)


class EgressTunnel:
    def __init__(
        self, gateway: EgressGateway, connect: Connect, *, idle: int = IDLE_CONNECTIONS
    ) -> None:
        self._gateway: EgressGateway = gateway
        self._connect: Connect = connect
        self._idle: int = idle
        self._workers: list[asyncio.Task[None]] = []
        self._serving: set[asyncio.Task[None]] = set()

    def start(self) -> None:
        self._workers = [
            asyncio.create_task(self._hold(), name=f"vm-egress-{i}") for i in range(self._idle)
        ]

    async def stop(self) -> None:
        tasks = [*self._workers, *self._serving]
        self._workers = []
        for task in tasks:
            await cancel_and_wait(task, STOP_TIMEOUT_S)

    async def _hold(self) -> None:
        failures = 0
        while True:
            try:
                reader, writer = await self._connect()
            except OSError:
                await asyncio.sleep(_RETRY_S[min(failures, len(_RETRY_S) - 1)])
                failures += 1
                continue
            try:
                channel = await reader.readexactly(1)
            except asyncio.IncompleteReadError, OSError:
                writer.close()
                await asyncio.sleep(_RETRY_S[min(failures, len(_RETRY_S) - 1)])
                failures += 1
                continue
            failures = 0
            task = asyncio.create_task(self._gateway.serve(channel, reader, writer))
            self._serving.add(task)
            task.add_done_callback(self._serving.discard)


class PreviewPort:
    """A host loopback port reaching one workspace's preview through the relay mux."""

    def __init__(self, key: str, connect_mux: Connect) -> None:
        self._key: str = key
        self._connect_mux: Connect = connect_mux
        self._server: asyncio.Server | None = None

    @property
    def port(self) -> int:
        if self._server is None:
            raise RuntimeError("the preview port is not open")
        return cast("tuple[str, int]", self._server.sockets[0].getsockname())[1]

    async def open(self) -> int:
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        return self.port

    async def close(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(server.wait_closed(), STOP_TIMEOUT_S)

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            up_r, up_w = await self._connect_mux()
            up_w.write(f"{self._key}\n".encode())
            await up_w.drain()
        except OSError:
            writer.close()
            return
        await splice(reader, writer, up_r, up_w)
