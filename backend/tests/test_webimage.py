"""The remote-image fetch: only real, bounded, publicly-addressed images get through.

The point of the service is that the *operator's browser* never makes this request, so
these assert the checks that make it safe to make it on their behalf — the SSRF guard
on every hop, the format proof, and the streaming byte cap.
"""

from __future__ import annotations

import socket
from collections.abc import Callable

import httpx
import pytest

from core.exceptions import SSRFError
from services.webimage import FetchedImage, ImageFetchError, fetch_image

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
GIF = b"GIF89a" + b"\x00" * 32
WEBP = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"\x00" * 32

type _Handler = Callable[[httpx.Request], httpx.Response]
type _AddrInfo = list[
    tuple[
        socket.AddressFamily,
        socket.SocketKind,
        int,
        str,
        tuple[str, int] | tuple[str, int, int, int],
    ]
]


def _resolves_to(monkeypatch: pytest.MonkeyPatch, ip: str) -> None:
    """Pin DNS so the guard sees ``ip`` for any host (offline + hermetic)."""

    def fake_getaddrinfo(
        _host: str | None, port: int | str | None, *_args: object, **_kwargs: object
    ) -> _AddrInfo:
        family = socket.AF_INET6 if ":" in ip else socket.AF_INET
        return [(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, int(port or 0)))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)


def _client(handler: _Handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)


def _serves(body: bytes, content_type: str = "image/png", status: int = 200) -> _Handler:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=body, headers={"content-type": content_type})

    return handler


async def _fetch(
    handler: _Handler, url: str = "https://pics.example/x.png", *, max_bytes: int = 1_000_000
) -> FetchedImage:
    async with _client(handler) as client:
        return await fetch_image(url, client=client, timeout_s=5.0, max_bytes=max_bytes)


async def test_returns_a_real_image(monkeypatch: pytest.MonkeyPatch):
    _resolves_to(monkeypatch, "93.184.216.34")
    image = await _fetch(_serves(PNG))
    assert image.content_type == "image/png"
    assert image.data == PNG


@pytest.mark.parametrize(
    ("body", "expected"),
    [(PNG, "image/png"), (GIF, "image/gif"), (WEBP, "image/webp")],
)
async def test_sniffs_each_supported_format(
    monkeypatch: pytest.MonkeyPatch, body: bytes, expected: str
):
    _resolves_to(monkeypatch, "93.184.216.34")
    image = await _fetch(_serves(body, content_type="image/png"))
    # The *sniffed* type wins over the remote's claim — a server that mislabels its
    # bytes must not get to pick the type the browser then trusts.
    assert image.content_type == expected


async def test_refuses_a_non_public_target(monkeypatch: pytest.MonkeyPatch):
    _resolves_to(monkeypatch, "127.0.0.1")
    with pytest.raises(SSRFError):
        _ = await _fetch(_serves(PNG))


async def test_refuses_a_redirect_to_a_private_address(monkeypatch: pytest.MonkeyPatch):
    # The open-redirect case: the first host is public, its Location is not. Following
    # redirects inside the client would have made the request before anyone looked.
    hops = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        hops["n"] += 1
        if hops["n"] == 1:
            return httpx.Response(302, headers={"location": "http://internal.example/x.png"})
        return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})

    def fake_getaddrinfo(
        host: str | None, port: int | str | None, *_args: object, **_kwargs: object
    ) -> _AddrInfo:
        ip = "93.184.216.34" if host == "pics.example" else "10.0.0.5"
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, int(port or 0)))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(SSRFError):
        _ = await _fetch(handler)


async def test_follows_a_redirect_to_a_public_address(monkeypatch: pytest.MonkeyPatch):
    _resolves_to(monkeypatch, "93.184.216.34")
    hops = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        hops["n"] += 1
        if hops["n"] == 1:
            return httpx.Response(302, headers={"location": "/moved.png"})
        return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})

    image = await _fetch(handler)
    assert image.data == PNG


async def test_rejects_html_dressed_as_an_image(monkeypatch: pytest.MonkeyPatch):
    _resolves_to(monkeypatch, "93.184.216.34")
    with pytest.raises(ImageFetchError):
        _ = await _fetch(_serves(b"<html>hi</html>", content_type="text/html"))


async def test_rejects_image_labelled_bytes_that_are_not_one(monkeypatch: pytest.MonkeyPatch):
    # The declared type is fine; the bytes are not. The magic-byte check is what
    # catches a payload wearing an image's content type.
    _resolves_to(monkeypatch, "93.184.216.34")
    with pytest.raises(ImageFetchError):
        _ = await _fetch(_serves(b"<svg onload=alert(1)>", content_type="image/png"))


async def test_rejects_svg(monkeypatch: pytest.MonkeyPatch):
    # SVG is a document, not a raster — deliberately outside the allowlist.
    _resolves_to(monkeypatch, "93.184.216.34")
    with pytest.raises(ImageFetchError):
        _ = await _fetch(_serves(b"<svg xmlns='...'/>", content_type="image/svg+xml"))


async def test_enforces_the_byte_cap(monkeypatch: pytest.MonkeyPatch):
    _resolves_to(monkeypatch, "93.184.216.34")
    with pytest.raises(ImageFetchError):
        _ = await _fetch(_serves(PNG + b"\x00" * 4096), max_bytes=512)


async def test_cap_is_enforced_on_the_stream_not_the_declared_length(
    monkeypatch: pytest.MonkeyPatch,
):
    # A hostile server can understate Content-Length; the cap has to hold anyway.
    _resolves_to(monkeypatch, "93.184.216.34")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=PNG + b"\x00" * 8192,
            headers={"content-type": "image/png", "content-length": "10"},
        )

    with pytest.raises(ImageFetchError):
        _ = await _fetch(handler, max_bytes=1024)


async def test_reports_an_http_error(monkeypatch: pytest.MonkeyPatch):
    _resolves_to(monkeypatch, "93.184.216.34")
    with pytest.raises(ImageFetchError):
        _ = await _fetch(_serves(b"", content_type="image/png", status=404))


async def test_refuses_an_endless_redirect_chain(monkeypatch: pytest.MonkeyPatch):
    _resolves_to(monkeypatch, "93.184.216.34")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://pics.example/again.png"})

    with pytest.raises(ImageFetchError):
        _ = await _fetch(handler)
