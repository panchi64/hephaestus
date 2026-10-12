"""Runs inside a workspace's confinement: wires up the network the command may use, then
runs it.

The confinement has its own network namespace holding nothing but loopback. This puts
the egress proxy on ``127.0.0.1:3128`` by bridging it to the egress socket bound in from
outside, and, for a preview, exposes the server's port on the preview socket the relay
reaches it through. Then it runs the command and exits with its status.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys

PROXY_PORT = 3128
EGRESS_SOCK = "/run/egress.sock"
PREVIEW_SOCK = "/run/preview/sock"


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


def _bridge(connect):
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            up_r, up_w = await connect()
        except OSError:
            writer.close()
            return
        await asyncio.gather(_pump(reader, up_w), _pump(up_r, writer), return_exceptions=True)

    return handle


async def main(args: argparse.Namespace) -> int:
    servers = [
        await asyncio.start_server(
            _bridge(lambda: asyncio.open_unix_connection(EGRESS_SOCK)), "127.0.0.1", PROXY_PORT
        )
    ]
    if args.publish:
        port = args.publish
        servers.append(
            await asyncio.start_unix_server(
                _bridge(lambda: asyncio.open_connection("127.0.0.1", port)), PREVIEW_SOCK
            )
        )
    proc = await asyncio.create_subprocess_exec(*args.argv)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, proc.send_signal, sig)
    code = await proc.wait()
    for server in servers:
        server.close()
    return 128 - code if code < 0 else code


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--publish", type=int, default=0)
    argv = sys.argv[1:]
    at = argv.index("--") if "--" in argv else len(argv)
    parsed = parser.parse_args(argv[:at])
    parsed.argv = argv[at + 1 :]
    if not parsed.argv:
        sys.exit(2)
    os.umask(0o022)
    sys.exit(asyncio.run(main(parsed)))
