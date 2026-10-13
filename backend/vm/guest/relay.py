"""The guest's only ways out: the egress tunnel and the preview mux.

The guest has no route to the host or the internet. The host keeps idle connections
open to the pool port; a proxy client inside the guest takes one and sends a channel
byte first, so the host knows which policy applies:

* ``W`` — a confined workspace command, reaching the egress socket. Its requests carry
  per-workspace proxy credentials, checked on the host.
* ``P`` — a platform service (Chromium, SearXNG) on the guest's own loopback, held to
  public addresses only.

The preview mux is the reverse direction: the host connects, names a workspace, and is
spliced to that workspace's preview socket.

Standard library only; it is copied into the guest at boot.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re

_KEY = re.compile(r"^[A-Za-z0-9_.-]{1,200}$")
#: How long a guest client waits for the host to supply a pool connection.
_TAKE_TIMEOUT_S = 30.0


async def _pump(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except OSError:
        pass
    finally:
        try:
            writer.close()
        except OSError:
            pass


async def _splice(a_r, a_w, b_r, b_w) -> None:
    await asyncio.gather(_pump(a_r, b_w), _pump(b_r, a_w), return_exceptions=True)


class Pool:
    def __init__(self) -> None:
        self._idle: asyncio.Queue[tuple[asyncio.StreamReader, asyncio.StreamWriter]] = (
            asyncio.Queue()
        )

    async def offer(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await self._idle.put((reader, writer))

    async def take(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _TAKE_TIMEOUT_S
        while True:
            reader, writer = await asyncio.wait_for(self._idle.get(), deadline - loop.time())
            if not reader.at_eof() and not writer.is_closing():
                return reader, writer


def _client(pool: Pool, channel: bytes):
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            up_r, up_w = await pool.take()
        except TimeoutError:
            writer.close()
            return
        try:
            up_w.write(channel)
            await up_w.drain()
        except OSError:
            writer.close()
            return
        await _splice(reader, writer, up_r, up_w)

    return handle


def _mux(previews: str):
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            key = (await asyncio.wait_for(reader.readline(), 10)).decode().strip()
        except (TimeoutError, UnicodeDecodeError, OSError):
            writer.close()
            return
        if not _KEY.match(key):
            writer.close()
            return
        try:
            up_r, up_w = await asyncio.open_unix_connection(os.path.join(previews, key, "sock"))
        except OSError:
            writer.close()
            return
        await _splice(reader, writer, up_r, up_w)

    return handle


async def main(args: argparse.Namespace) -> None:
    pool = Pool()
    run = os.path.join(args.root, "run/hephaestus")
    sock = os.path.join(run, "egress.sock")
    os.makedirs(run, exist_ok=True)
    if os.path.exists(sock):
        os.unlink(sock)
    servers = [
        await asyncio.start_server(pool.offer, args.listen, args.pool_port),
        await asyncio.start_server(_mux(os.path.join(run, "previews")), args.listen, args.mux_port),
        await asyncio.start_server(_client(pool, b"P"), "127.0.0.1", args.proxy_port),
        await asyncio.start_unix_server(_client(pool, b"W"), sock),
    ]
    os.chmod(sock, 0o666)
    print("RELAY-READY", flush=True)
    await asyncio.gather(*(server.serve_forever() for server in servers))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default="/")
    parser.add_argument("--listen", default="0.0.0.0")
    parser.add_argument("--pool-port", type=int, default=7101)
    parser.add_argument("--mux-port", type=int, default=7102)
    parser.add_argument("--proxy-port", type=int, default=3128)
    asyncio.run(main(parser.parse_args()))
