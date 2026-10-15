"""The remote-image proxy endpoint: wiring, headers, and how failures surface.

The service's own checks are covered in ``test_webimage``; this asserts the route is
registered, that it serves the bytes inert, and that a refusal and a fetch failure
arrive as different statuses — the frontend distinguishes "never retry" from "the
remote had a bad day".
"""

from __future__ import annotations

import socket
from collections.abc import Callable

import httpx
import pytest
from fastapi import FastAPI

from routes.deps import app_state

from ._helpers import client_app

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def _resolves_to(monkeypatch: pytest.MonkeyPatch, ip: str) -> None:
    def fake_getaddrinfo(
        _host: str | None, port: int | None, *_args: object, **_kwargs: object
    ) -> list[tuple[socket.AddressFamily, socket.SocketKind, int, str, tuple[str, int]]]:
        family = socket.AF_INET6 if ":" in ip else socket.AF_INET
        return [(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, port or 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)


def _serve(app: FastAPI, handler: Callable[[httpx.Request], httpx.Response]) -> None:
    """Swap the web feature's outbound client for a mocked transport."""
    app_state(app).web_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=False
    )


async def test_serves_the_image_inert(monkeypatch: pytest.MonkeyPatch):
    _resolves_to(monkeypatch, "93.184.216.34")
    async with client_app() as (client, app):
        _serve(
            app,
            lambda r: httpx.Response(200, content=PNG, headers={"content-type": "image/png"}),
        )
        resp = await client.get("/media/remote-image", params={"url": "https://pics.example/c.png"})
        assert resp.status_code == 200
        assert resp.content == PNG
        assert resp.headers["content-type"].startswith("image/png")
        # Inert: the browser is pinned to the sniffed type and the response is
        # granted nothing, in case the format check was ever wrong.
        assert resp.headers["x-content-type-options"] == "nosniff"
        assert "default-src 'none'" in resp.headers["content-security-policy"]
        assert resp.headers["referrer-policy"] == "no-referrer"


async def test_a_refused_target_is_distinct_from_a_failed_fetch(monkeypatch: pytest.MonkeyPatch):
    _resolves_to(monkeypatch, "127.0.0.1")
    async with client_app() as (client, app):
        _serve(app, lambda r: httpx.Response(200, content=PNG))
        resp = await client.get(
            "/media/remote-image", params={"url": "http://localhost/secret.png"}
        )
        # 403, not 502: the frontend must never retry a boundary refusal.
        assert resp.status_code == 403


async def test_a_non_image_response_is_a_bad_gateway(monkeypatch: pytest.MonkeyPatch):
    _resolves_to(monkeypatch, "93.184.216.34")
    async with client_app() as (client, app):
        _serve(
            app,
            lambda r: httpx.Response(
                200, content=b"<html></html>", headers={"content-type": "text/html"}
            ),
        )
        resp = await client.get("/media/remote-image", params={"url": "https://pics.example/c.png"})
        assert resp.status_code == 502


@pytest.mark.parametrize("url", ["", "not-a-url", "javascript:alert(1)"])
async def test_rejects_a_url_that_is_not_a_fetchable_address(
    monkeypatch: pytest.MonkeyPatch, url: str
):
    _resolves_to(monkeypatch, "93.184.216.34")
    async with client_app() as (client, app):
        _serve(app, lambda r: httpx.Response(200, content=PNG))
        resp = await client.get("/media/remote-image", params={"url": url})
        assert resp.status_code in (403, 422)
