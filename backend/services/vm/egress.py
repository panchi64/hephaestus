"""The allowlisting proxy every connection out of the platform VM ends at.

It runs in the backend, at the far end of the egress tunnel (:mod:`services.vm.tunnel`).
Each tunnelled connection arrives with a channel byte saying who opened it:

* **workspace** — a confined command. Its requests must carry the proxy credentials its
  workspace was granted (:meth:`EgressGateway.grant`), and only the domains on that
  workspace's allowlist are reachable. The allowlist file is re-read whenever it
  changes, so an approval takes effect on the next request.
* **platform** — Chromium or SearXNG. Any public host is reachable.

Either way the destination is resolved here and dialled by a pinned public address, so
an allowed name that resolves into the operator's LAN, the host's loopback or a cloud
metadata service is refused. Compute and package installs stay unrestricted; what the
fence controls is where bytes may go.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import ipaddress
import logging
import secrets
import socket
import ssl
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from core.ssrf import is_blocked

if TYPE_CHECKING:
    from services.vm.intercept import Interceptor, Secret

logger = logging.getLogger(__name__)

#: The file the egress policy (:mod:`services.egress`) writes in each allowlist directory.
ALLOW_FILE = "allow.txt"
WORKSPACE = b"W"
PLATFORM = b"P"
#: Written into a refusal's body so a denial is legible in whatever the agent ran,
#: rather than an unexplained 403 from "some proxy".
DENIED_MARKER = "odysseus-egress: denied"
#: The most of a request head the proxy holds before answering. What sends it is code the
#: agent wrote, so an endless head is refused rather than buffered.
_MAX_HEAD_BYTES = 64 * 1024
HEAD_TIMEOUT_S = 30.0


def parse_allowlist(text: str) -> tuple[str, ...]:
    """The domains in an ``allow.txt``: one per line, blanks dropped, case folded."""
    return tuple(entry for line in text.splitlines() if (entry := line.strip().lower()))


def allowed(host: str, domains: Iterable[str]) -> bool:
    """Whether ``host`` is covered by the allowlist.

    A plain entry is that host and nothing else; ``*.example.com`` covers subdomains and
    only those. That is the reading the host confinement gives the same list and the one
    the approval card states, so an approval never grants more than it said.
    """
    name = host.strip().rstrip(".").lower()
    for entry in domains:
        if entry.startswith("*."):
            base = entry[2:]
            if base and name.endswith(f".{base}"):
                return True
        elif name == entry:
            return True
    return False


class Policy(Protocol):
    def permits(self, host: str) -> bool: ...


class PublicPolicy:
    def permits(self, host: str) -> bool:
        return True


class Allowlist:
    """A workspace's allowlist file, re-read whenever its mtime moves. A missing file
    allows nothing."""

    def __init__(self, directory: Path) -> None:
        self._path = directory / ALLOW_FILE
        self._stamp: int | None = None
        self._domains: tuple[str, ...] = ()

    def domains(self) -> tuple[str, ...]:
        try:
            stamp = self._path.stat().st_mtime_ns
        except OSError:
            return ()
        if stamp != self._stamp:
            try:
                self._domains = parse_allowlist(self._path.read_text("utf-8"))
            except OSError:
                return ()
            self._stamp = stamp
        return self._domains

    def permits(self, host: str) -> bool:
        return allowed(host, self.domains())


@dataclass(frozen=True)
class Grant:
    """What one set of workspace credentials reaches, and the secrets sent for it."""

    allowlist: Policy
    secrets: tuple[Secret, ...] = ()

    def permits(self, host: str) -> bool:
        return self.allowlist.permits(host)

    def secrets_for(self, host: str) -> tuple[Secret, ...]:
        name = host.strip().rstrip(".").lower()
        return tuple(secret for secret in self.secrets if secret.host == name)


class EgressGateway:
    """Serves tunnelled connections, deciding each by its channel and credentials."""

    def __init__(self, interceptor: Interceptor | None = None) -> None:
        self._grants: dict[str, Grant] = {}
        self._standing: dict[Path, str] = {}
        self._public = Grant(PublicPolicy())
        self.interceptor = interceptor

    def grant(self, allow_dir: Path, secrets_: Sequence[Secret] = ()) -> str:
        """Proxy credentials (``user:password``) that reach what ``allow_dir`` allows,
        with ``secrets_`` put into HTTPS requests to their hosts."""
        token = secrets.token_urlsafe(24)
        self._grants[token] = Grant(Allowlist(allow_dir), tuple(secrets_))
        return f"workspace:{token}"

    def credentials_for(self, allow_dir: Path) -> str:
        """Standing credentials for ``allow_dir``, the same for every caller: for the
        project shell, whose commands have no session to tie a grant's lifetime to."""
        credentials = self._standing.get(allow_dir)
        if credentials is None:
            credentials = self._standing[allow_dir] = self.grant(allow_dir)
        return credentials

    def revoke(self, credentials: str) -> None:
        self._grants.pop(credentials.partition(":")[2], None)

    async def serve(
        self, channel: bytes, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            request = await asyncio.wait_for(read_head(reader), HEAD_TIMEOUT_S)
            if request is None:
                await _respond(writer, "431 Request Header Fields Too Large")
                return
            if not request.line:
                writer.close()
                return
            grant = self._grant(channel, request)
            if grant is None:
                await _respond(
                    writer,
                    "407 Proxy Authentication Required",
                    "odysseus-egress: this workspace's proxy credentials are missing\n",
                    extra='Proxy-Authenticate: Basic realm="odysseus"\r\n',
                )
                return
            await self._dispatch(request, grant, reader, writer)
        except Exception:  # one bad client must never stop the gateway
            logger.debug("egress: a tunnelled connection failed", exc_info=True)
            _close(writer)

    def _grant(self, channel: bytes, request: _Request) -> Grant | None:
        if channel == PLATFORM:
            return self._public
        if channel != WORKSPACE:
            return None
        token = _basic_password(request.header(b"proxy-authorization"))
        return self._grants.get(token or "")

    async def _dispatch(
        self,
        request: _Request,
        grant: Grant,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        parts = request.line.decode("latin-1").split()
        if len(parts) != 3:
            await _respond(writer, "400 Bad Request")
            return
        method, target, version = parts
        if method.upper() != "CONNECT":
            await _forward(method, target, version, request.headers, grant, reader, writer)
            return
        host, port = split_hostport(target, 443)
        if not grant.permits(host):
            await _deny(writer, host)
            return
        secrets_ = grant.secrets_for(host)
        if secrets_ and self.interceptor is not None:
            await self.interceptor.intercept(host, port, secrets_, reader, writer)
        else:
            await _connect(host, port, reader, writer)


class _Request:
    def __init__(self, line: bytes, headers: list[bytes]) -> None:
        self.line = line
        self.headers = headers

    def header(self, name: bytes) -> bytes | None:
        prefix = name + b":"
        for line in self.headers:
            if line.lower().startswith(prefix):
                return line[len(prefix) :].strip()
        return None


async def read_head(reader: asyncio.StreamReader) -> _Request | None:
    line = await reader.readline()
    if not line:
        return _Request(b"", [])
    headers: list[bytes] = []
    received = len(line)
    while (header := await reader.readline()) not in (b"\r\n", b"\n", b""):
        received += len(header)
        if received > _MAX_HEAD_BYTES:
            return None
        headers.append(header)
    return _Request(line, headers)


def _basic_password(value: bytes | None) -> str | None:
    if not value:
        return None
    scheme, _, encoded = value.partition(b" ")
    if scheme.lower() != b"basic":
        return None
    try:
        decoded = base64.b64decode(encoded.strip(), validate=True).decode()
    except binascii.Error, UnicodeDecodeError:
        return None
    return decoded.partition(":")[2]




async def _connect(
    host: str, port: int, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    upstream = await dial_pinned(host, port, writer)
    if upstream is None:
        return
    server_r, server_w = upstream
    writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
    await writer.drain()
    await splice(reader, writer, server_r, server_w)


async def _forward(
    method: str,
    target: str,
    version: str,
    headers: list[bytes],
    policy: Policy,
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    # Absolute-form only: `GET http://host/path HTTP/1.1`. The client's own `Host:` is
    # dropped and re-stated from the request line, so the host that was checked is the
    # host that is asked; one request per connection, so no keep-alive framing to track.
    scheme, marker, rest = target.partition("://")
    if not marker or scheme != "http":
        await _respond(writer, "400 Bad Request")
        return
    authority, _, path = rest.partition("/")
    host, port = split_hostport(authority, 80)
    if not policy.permits(host):
        await _deny(writer, host)
        return
    upstream = await dial_pinned(host, port, writer)
    if upstream is None:
        return
    server_r, server_w = upstream
    server_w.write(f"{method} /{path} {version}\r\nHost: {authority}\r\n".encode())
    for line in headers:
        if not line.lower().startswith((b"proxy-", b"connection:", b"host:")):
            server_w.write(line)
    server_w.write(b"Connection: close\r\n\r\n")
    await server_w.drain()
    await splice(reader, writer, server_r, server_w)


def split_hostport(authority: str, default_port: int) -> tuple[str, int]:
    """Parse ``host:port`` (or a bare host), handling ``[ipv6]:port`` brackets."""
    if authority.startswith("["):
        host, _, rest = authority[1:].partition("]")
        port = int(rest[1:]) if rest.startswith(":") and rest[1:].isdigit() else default_port
        return host, port
    if ":" in authority:
        host, _, p = authority.rpartition(":")
        return host, int(p) if p.isdigit() else default_port
    return authority, default_port


async def _resolve_public_ips(host: str, port: int) -> list[str]:
    """Resolve ``host``, refusing it if *any* answer is non-public: a split-horizon name
    with one private answer is a way back in."""
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    addrs: list[str] = []
    for info in infos:
        addr = str(info[4][0])
        if is_blocked(ipaddress.ip_address(addr)):
            raise PermissionError(f"{addr} is non-public")
        if addr not in addrs:
            addrs.append(addr)
    if not addrs:
        raise OSError("did not resolve")
    return addrs


async def dial_pinned(
    host: str, port: int, client_w: asyncio.StreamWriter, *, tls: ssl.SSLContext | None = None
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter] | None:
    """Connect to a pinned public address of ``host``, trying each in turn, over
    ``tls`` verified as ``host`` when given; ``None`` after refusing ``client_w``."""
    try:
        ips = await _resolve_public_ips(host, port)
    except PermissionError:
        await _deny(client_w, host)
        return None
    except OSError:
        await _respond(client_w, "502 Bad Gateway")
        return None
    for ip in ips:
        try:
            return await asyncio.open_connection(
                host=ip, port=port, ssl=tls, server_hostname=host if tls else None
            )
        except OSError:
            continue
    await _respond(client_w, "502 Bad Gateway")
    return None


async def _pump(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except OSError:
        pass
    finally:
        _close(writer)


async def splice(a_r, a_w, b_r, b_w) -> None:
    await asyncio.gather(_pump(a_r, b_w), _pump(b_r, a_w), return_exceptions=True)


def _close(writer: asyncio.StreamWriter) -> None:
    try:
        writer.close()
    except OSError:
        pass


async def _respond(
    writer: asyncio.StreamWriter, status: str, body: str = "", *, extra: str = ""
) -> None:
    payload = body.encode()
    head = (
        f"HTTP/1.1 {status}\r\n{extra}"
        f"Content-Type: text/plain\r\nContent-Length: {len(payload)}\r\n"
        "Connection: close\r\n\r\n"
    )
    try:
        writer.write(head.encode() + payload)
        await writer.drain()
    except OSError:
        pass
    finally:
        _close(writer)


async def _deny(writer: asyncio.StreamWriter, host: str) -> None:
    """Refuse ``host`` with :data:`DENIED_MARKER` in the body."""
    logger.info("egress: denied %s", host)
    await _respond(writer, "403 Forbidden", f"{DENIED_MARKER} {host}\n")
