"""Outbound-request guard against server-side request forgery (SSRF).

Any URL the agent hands us to fetch is untrusted: left unchecked it could point at
the loopback interface, the private LAN, or a cloud instance-metadata endpoint and
turn the backend into a confused deputy. Before any outbound fetch — and again on
every redirect hop — the target host is resolved and every resulting address is
checked; if any is non-public the request is refused.

Platform-agnostic: pure stdlib (``socket`` + ``ipaddress``), no OS-specific facility.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit

from core.exceptions import SSRFError

_ALLOWED_SCHEMES = {"http", "https"}
# The cloud instance-metadata address (AWS/GCP/Azure/OpenStack all answer here).
# It is link-local, so ``is_link_local`` already covers it — listed for intent.
_METADATA_ADDRESSES = frozenset({"169.254.169.254", "fd00:ec2::254"})
# Shared address space (RFC 6598) — carrier-grade NAT and Tailscale meshes live
# here. Python's ``is_private`` does NOT cover it, so block it explicitly: on a
# self-hosted LAN it routes to neighbouring hosts the guard is meant to refuse.
_SHARED_ADDRESS_SPACE = ipaddress.ip_network("100.64.0.0/10")


def is_blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    # Unwrap IPv4-mapped IPv6 (e.g. ::ffff:127.0.0.1) so the v4 checks apply.
    mapped: ipaddress.IPv4Address | None = getattr(ip, "ipv4_mapped", None)
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


async def assert_public_url(url: str) -> None:
    """Refuse ``url`` unless it is http(s) and every address its host resolves to
    is publicly routable. Raises :class:`SSRFError` otherwise.

    DNS resolution runs in a thread (``getaddrinfo`` is blocking). Call this for
    the initial URL *and* for each redirect ``Location`` before following it — a
    public host can redirect to a private one (open-redirect).

    Known limitation — DNS rebinding: this validates the addresses ``getaddrinfo``
    returns now, but the HTTP client re-resolves the host when it connects, so a
    low-TTL attacker domain could return a public IP here and a private one at
    connect time. Fully closing this needs the connection pinned to the validated
    IP (a custom resolver/transport); until then the guard stops the direct and
    open-redirect cases, not a timed rebind.
    """
    parts = urlsplit(url)
    if parts.scheme not in _ALLOWED_SCHEMES:
        raise SSRFError(f"refused to fetch {url!r}: scheme {parts.scheme!r} is not allowed")
    # Embedded credentials (user:pass@host) would be sent to the host as auth —
    # a public host could then harvest a secret the URL was tricked into carrying.
    if parts.username or parts.password:
        raise SSRFError(f"refused to fetch {url!r}: URLs with embedded credentials are not allowed")
    host = parts.hostname
    if not host:
        raise SSRFError(f"refused to fetch {url!r}: no host")

    loop = asyncio.get_running_loop()
    try:
        infos = await loop.run_in_executor(
            None, lambda: socket.getaddrinfo(host, parts.port, proto=socket.IPPROTO_TCP)
        )
    except socket.gaierror as exc:
        raise SSRFError(f"refused to fetch {url!r}: host {host!r} did not resolve") from exc

    addresses = {info[4][0] for info in infos}
    if not addresses:
        raise SSRFError(f"refused to fetch {url!r}: host {host!r} did not resolve")
    for addr in addresses:
        # An address this can't parse is one it can't prove is public, so it is refused
        # rather than passed through — the guard fails closed. (Scoped link-local forms
        # like ``fe80::1%eth0`` do parse, and land in `is_blocked` on their own merits.)
        try:
            parsed = ipaddress.ip_address(addr)
        except ValueError as exc:
            raise SSRFError(
                f"refused to fetch {url!r}: host {host!r} resolved to unparseable address {addr!r}"
            ) from exc
        if is_blocked(parsed):
            raise SSRFError(
                f"refused to fetch {url!r}: host {host!r} resolves to non-public address {addr}"
            )
