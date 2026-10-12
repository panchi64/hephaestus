"""SSRF-enforcing forward proxy — runs as a host subprocess, NOT in the backend process.

The host browser (``services/browser/host.py``) must have its SSRF policy enforced on
every request, but doing that the obvious way (a Playwright ``context.route`` handler)
enables CDP's ``Fetch`` domain — which bot walls (Reddit/Cloudflare class) detect and
hard-block. So enforcement moves *out of the browser*: this is a plain CONNECT/HTTP forward
proxy that the browser is pointed at with ``--proxy-server``. It runs as its own process
on the host's loopback, resolves each destination, **pins the resolved public IP**
(closing the DNS-rebinding gap the in-browser guard couldn't), and refuses any non-public
address. TLS stays end-to-end browser↔site (CONNECT is an opaque tunnel — no interception,
no CA, no proxy tell).

**Self-contained on purpose** — it is run by path with none of our code on its path, so it
imports stdlib only. The SSRF predicate below mirrors
``core.ssrf.is_blocked``; ``tests/test_webfetch.py`` asserts the two never drift.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import sys

# Mirrors core.ssrf — keep in lockstep (a test enforces parity).
_SHARED_ADDRESS_SPACE = ipaddress.ip_network("100.64.0.0/10")  # RFC 6598 CGNAT / Tailscale
_METADATA_ADDRESSES = frozenset({"169.254.169.254", "fd00:ec2::254"})


def _is_blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
        or (ip.version == 4 and ip in _SHARED_ADDRESS_SPACE)
        or str(ip) in _METADATA_ADDRESSES
    )


async def _resolve_public_ips(host: str, port: int) -> list[str]:
    """Resolve ``host`` to the list of IPs to connect to, or raise if *any* address it
    resolves to is non-public (conservative: a split-horizon name with one private answer is
    refused). The caller dials a returned literal, which pins it — no second resolution to
    rebind. Order is preserved so the caller can fall through families (a host may be IPv4-only,
    yet getaddrinfo may list an unreachable IPv6 first)."""
    loop = asyncio.get_running_loop()
    infos = await loop.run_in_executor(
        None, lambda: socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    )
    addrs: list[str] = []
    for info in infos:
        addr = str(info[4][0])
        if _is_blocked(ipaddress.ip_address(addr)):
            raise PermissionError(f"{addr} is non-public")
        if addr not in addrs:
            addrs.append(addr)
    if not addrs:
        raise OSError("did not resolve")
    return addrs


async def _dial_pinned(host: str, port: int, client_w: asyncio.StreamWriter):
    """The load-bearing security step, shared by CONNECT and plain HTTP: resolve ``host``,
    refuse if non-public (403), then connect to a pinned public IP — trying each in order so
    an unreachable family doesn't dead-end a reachable one. Returns the upstream
    (reader, writer) on success, or ``None`` after having sent the refusal to ``client_w``."""
    try:
        ips = await _resolve_public_ips(host, port)
    except PermissionError:
        await _refuse(client_w, "403 Forbidden")  # SSRF policy refusal
        return None
    except OSError:
        await _refuse(client_w, "502 Bad Gateway")
        return None
    for ip in ips:
        try:
            return await asyncio.open_connection(host=ip, port=port)
        except OSError:
            continue
    await _refuse(client_w, "502 Bad Gateway")
    return None


def _split_hostport(authority: str, default_port: int) -> tuple[str, int]:
    """Parse ``host:port`` (or a bare host), handling ``[ipv6]:port`` brackets."""
    if authority.startswith("["):
        host, _, rest = authority[1:].partition("]")
        port = int(rest[1:]) if rest.startswith(":") and rest[1:].isdigit() else default_port
        return host, port
    if ":" in authority:
        host, _, p = authority.rpartition(":")
        return host, int(p) if p.isdigit() else default_port
    return authority, default_port


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


async def _relay(client_r, client_w, server_r, server_w) -> None:
    await asyncio.gather(
        _pump(client_r, server_w), _pump(server_r, client_w), return_exceptions=True
    )


async def _refuse(writer: asyncio.StreamWriter, status: str) -> None:
    try:
        writer.write(f"HTTP/1.1 {status}\r\nConnection: close\r\n\r\n".encode())
        await writer.drain()
    except OSError:
        pass
    finally:
        writer.close()


async def _handle_connect(authority: str, client_r, client_w) -> None:
    host, port = _split_hostport(authority, 443)
    upstream = await _dial_pinned(host, port, client_w)
    if upstream is None:
        return
    server_r, server_w = upstream
    client_w.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
    await client_w.drain()
    await _relay(client_r, client_w, server_r, server_w)


async def _handle_http(method: str, target: str, version: str, client_r, client_w) -> None:
    # Absolute-form request line: `GET http://host/path HTTP/1.1`. Refuse anything else.
    if "://" not in target:
        await _refuse(client_w, "400 Bad Request")
        return
    scheme, _, rest = target.partition("://")
    if scheme != "http":
        await _refuse(client_w, "400 Bad Request")
        return
    authority, _, path = rest.partition("/")
    host, port = _split_hostport(authority, 80)
    # Read the rest of the request head; forward it origin-form, forcing a single
    # request per connection (Connection: close) so we needn't track keep-alive framing.
    head = bytearray()
    while (line := await client_r.readline()) not in (b"\r\n", b"", b"\n"):
        lower = line.lower()
        if lower.startswith(b"proxy-") or lower.startswith(b"connection:"):
            continue
        head += line
    upstream = await _dial_pinned(host, port, client_w)
    if upstream is None:
        return
    server_r, server_w = upstream
    server_w.write(f"{method} /{path} {version}\r\n".encode())
    server_w.write(bytes(head))
    server_w.write(b"Connection: close\r\n\r\n")
    await server_w.drain()
    await _relay(client_r, client_w, server_r, server_w)


async def _dispatch(client_r: asyncio.StreamReader, client_w: asyncio.StreamWriter) -> None:
    try:
        request_line = await client_r.readline()
        if not request_line:
            client_w.close()
            return
        parts = request_line.decode("latin-1").split()
        if len(parts) != 3:
            await _refuse(client_w, "400 Bad Request")
            return
        method, target, version = parts
        if method.upper() == "CONNECT":
            # Discard the CONNECT request's headers before tunnelling (EOF ends the loop).
            while (await client_r.readline()) not in (b"\r\n", b"", b"\n"):
                pass
            await _handle_connect(target, client_r, client_w)
        else:
            await _handle_http(method, target, version, client_r, client_w)
    except Exception:
        try:
            client_w.close()
        except OSError:
            pass


async def main(port: int) -> None:
    server = await asyncio.start_server(_dispatch, "127.0.0.1", port)
    # The owning process polls this line to know the proxy is listening (fail-closed: no
    # 'ready' ⇒ web fetch stays unavailable, never fetches without enforcement).
    print("PROXY-READY", flush=True)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 3128))
