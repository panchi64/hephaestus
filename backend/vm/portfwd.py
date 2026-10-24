"""Exposes a service bound to the guest's loopback on a port the host can forward to.

Chromium only serves its debugging port on loopback. A published port reaches the guest's
network address, so this listens there and relays to the loopback port.

Standard library only; ``build_image.sh`` installs it at ``/opt/hephaestus/portfwd.py``.
"""

from __future__ import annotations

import asyncio
import sys


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


async def main(listen: int, target: int) -> None:
    async def relay(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            up_r, up_w = await asyncio.open_connection("127.0.0.1", target)
        except OSError:
            writer.close()
            return
        _ = await asyncio.gather(_pump(reader, up_w), _pump(up_r, writer), return_exceptions=True)

    server = await asyncio.start_server(relay, "0.0.0.0", listen)
    await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1]), int(sys.argv[2])))
