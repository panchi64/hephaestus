"""The SSRF guard: refuse outbound fetches to non-public addresses or bad schemes."""

from __future__ import annotations

import socket
from typing import NoReturn

import pytest

from core.exceptions import SSRFError
from core.ssrf import assert_public_url


def _resolves_to(monkeypatch: pytest.MonkeyPatch, ip: str) -> None:
    """Pin DNS resolution so the guard sees ``ip`` for any host (offline + hermetic)."""

    def fake_getaddrinfo(
        _host: object, port: int | None, *_args: object, **_kwargs: object
    ) -> list[tuple[socket.AddressFamily, socket.SocketKind, int, str, tuple[str, int]]]:
        family = socket.AF_INET6 if ":" in ip else socket.AF_INET
        return [(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, port or 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)


async def test_allows_a_public_address(monkeypatch: pytest.MonkeyPatch):
    _resolves_to(monkeypatch, "93.184.216.34")  # example.com
    await assert_public_url("https://example.com/page")  # no raise


@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",  # loopback
        "10.0.0.5",  # private (RFC1918)
        "192.168.1.10",  # private
        "169.254.169.254",  # link-local / cloud metadata
        "::1",  # IPv6 loopback
        "fc00::1",  # IPv6 unique-local
        "::ffff:127.0.0.1",  # IPv4-mapped loopback
        "100.64.0.1",  # CGNAT / shared address space (RFC 6598)
        "0.0.0.0",  # unspecified
    ],
)
async def test_blocks_non_public_addresses(monkeypatch: pytest.MonkeyPatch, ip: str):
    _resolves_to(monkeypatch, ip)
    with pytest.raises(SSRFError):
        await assert_public_url("http://target.example/")


async def test_blocks_urls_with_embedded_credentials(monkeypatch: pytest.MonkeyPatch):
    # Host is public, but the userinfo would be sent as auth to it — refuse outright.
    _resolves_to(monkeypatch, "93.184.216.34")
    with pytest.raises(SSRFError):
        await assert_public_url("http://secret:token@public.example/")


async def test_blocks_non_http_schemes(monkeypatch: pytest.MonkeyPatch):
    _resolves_to(monkeypatch, "93.184.216.34")
    for url in ("ftp://example.com/x", "file:///etc/passwd", "gopher://example.com"):
        with pytest.raises(SSRFError):
            await assert_public_url(url)


async def test_blocks_unresolvable_host(monkeypatch: pytest.MonkeyPatch):
    def boom(*_args: object, **_kwargs: object) -> NoReturn:
        raise socket.gaierror("name resolution failed")

    monkeypatch.setattr(socket, "getaddrinfo", boom)
    with pytest.raises(SSRFError):
        await assert_public_url("https://does-not-resolve.example/")
