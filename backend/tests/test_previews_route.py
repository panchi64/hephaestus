"""The token-gated preview reverse proxy: HTTP and WebSocket forwarding, the
credential stripping in both directions, token gating, and the auth-gate exemption.

Real upstream servers on loopback ports stand in for a sandbox dev server; a fake
session manager resolves a token to a handle pointing at them."""

from __future__ import annotations

import asyncio
import contextlib
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, override

from starlette.testclient import TestClient
from websockets.asyncio.server import serve

from app import create_app
from core.config import Settings
from services.sandbox import PreviewHandle

from ._helpers import client_app


class _FakeManager:
    """Resolves exactly one token to a handle aimed at a real upstream port."""

    def __init__(self, token: str, host_port: int) -> None:
        self._token = token
        self._handle = PreviewHandle(
            token=token,
            host_port=host_port,
            server_port=8000,
            command=("srv",),
        )
        self.touches = 0

    def resolve_preview(self, token: str) -> PreviewHandle | None:
        if token != self._token:
            return None
        self.touches += 1
        return self._handle


@contextlib.contextmanager
def _http_upstream(record: dict[str, str | None], extra_headers: dict[str, str] | None = None):
    """A loopback HTTP server that echoes method+path, sets a cookie, and records
    what it received — to prove the proxy strips credentials and cookies.

    ``extra_headers`` lets a test have the upstream assert its own headers (its own
    CSP, say) so the proxy's handling of them is observable."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            record["cookie"] = self.headers.get("cookie")
            record["authorization"] = self.headers.get("authorization")
            record["xfp"] = self.headers.get("x-forwarded-prefix")
            body = f"{self.command} {self.path}".encode()
            self.send_response(200)
            self.send_header("content-type", "text/plain")
            self.send_header("set-cookie", "evil=1")  # must be stripped on the way out
            for name, value in (extra_headers or {}).items():
                self.send_header(name, value)
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        @override
        def log_message(self, format: str, *args: Any) -> None:  # silence the server
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()


@contextlib.contextmanager
def _ws_upstream():
    """A loopback WebSocket echo server (its own thread + loop)."""
    port_box: dict[str, int] = {}
    started = threading.Event()

    def run() -> None:
        async def main() -> None:
            async def echo(ws) -> None:
                async for message in ws:
                    await ws.send(message)

            server = await serve(echo, "127.0.0.1", 0)
            port_box["port"] = server.sockets[0].getsockname()[1]
            started.set()
            await asyncio.Future()  # serve until the daemon thread dies with the process

        asyncio.run(main())

    threading.Thread(target=run, daemon=True).start()
    assert started.wait(5)
    yield port_box["port"]


# --- HTTP leg ----------------------------------------------------------------


async def test_http_proxy_forwards_and_strips_credentials():
    record: dict[str, str | None] = {}
    with _http_upstream(record) as port:
        async with client_app() as (client, app):
            app.state.sandbox = _FakeManager("tok", port)
            resp = await client.get(
                "/previews/tok/hello?x=1",
                headers={"cookie": "hephaestus_session=secret", "authorization": "Bearer xyz"},
            )
            assert resp.status_code == 200
            assert resp.text == "GET /hello?x=1"  # method, path, and query forwarded
            assert resp.headers["x-content-type-options"] == "nosniff"
            assert "set-cookie" not in {k.lower() for k in resp.headers}  # not planted on us
    # The operator's credentials never reached the model's server.
    assert record["cookie"] is None
    assert record["authorization"] is None
    assert record["xfp"] == "/previews/tok"  # the prefix is advertised instead


async def test_proxied_html_is_sandboxed_by_csp_not_by_the_client():
    # The preview serves agent-authored HTML from the API origin, auth-exempt. Opened
    # top-level, a page that fetches /conversations would be same-origin with the API
    # and the session cookie is samesite=lax — so isolation cannot be left to the
    # frontend choosing a sandboxed iframe. The backend is the enforcement point.
    record: dict[str, str | None] = {}
    with _http_upstream(record) as port:
        async with client_app() as (client, app):
            app.state.sandbox = _FakeManager("tok", port)
            resp = await client.get("/previews/tok/index.html")

            csp = resp.headers["content-security-policy"]
            assert csp.startswith("sandbox")
            # allow-same-origin is what would hand the page the API's origin back.
            assert "allow-same-origin" not in csp
            assert resp.headers["x-content-type-options"] == "nosniff"


async def test_upstream_cannot_weaken_the_preview_sandbox():
    # A dev server that sets its own permissive CSP must not be able to relax ours:
    # the upstream header is stripped, never merged.
    record: dict[str, str | None] = {}
    with _http_upstream(record, extra_headers={"content-security-policy": "default-src *"}) as port:
        async with client_app() as (client, app):
            app.state.sandbox = _FakeManager("tok", port)
            resp = await client.get("/previews/tok/index.html")
            assert resp.headers["content-security-policy"].startswith("sandbox")
            assert "default-src *" not in resp.headers["content-security-policy"]


async def test_unknown_token_is_404():
    async with client_app() as (client, app):
        app.state.sandbox = _FakeManager("tok", 1)
        assert (await client.get("/previews/wrong/x")).status_code == 404


async def test_missing_manager_is_404():
    async with client_app() as (client, app):
        app.state.sandbox = None
        assert (await client.get("/previews/tok/x")).status_code == 404


async def test_previews_subtree_is_public():
    # Auth on, no credentials: a gated route would 401. Reaching the route (404 on
    # an unknown token) proves the token-gated subtree is exempt from the cookie gate.
    async with client_app(auth_enabled=True, passphrase="pw") as (client, app):
        app.state.sandbox = _FakeManager("tok", 1)
        assert (await client.get("/previews/wrong/x")).status_code == 404


# --- WebSocket leg -----------------------------------------------------------


def test_ws_proxy_round_trips_text_and_bytes(tmp_path):
    with _ws_upstream() as port:
        settings = Settings(
            db_url="sqlite:///:memory:",
            data_dir=tmp_path,
            auth_enabled=False,
            unlock_passphrase="pw",
        )
        app = create_app(settings)
        with TestClient(app) as client:
            app.state.sandbox = _FakeManager("tok", port)
            with client.websocket_connect("/previews/tok/ws") as ws:
                ws.send_text("ping")
                assert ws.receive_text() == "ping"
                ws.send_bytes(b"\x01\x02\x03")
                assert ws.receive_bytes() == b"\x01\x02\x03"


def test_ws_unknown_token_is_rejected(tmp_path):
    from starlette.websockets import WebSocketDisconnect

    settings = Settings(
        db_url="sqlite:///:memory:",
        data_dir=tmp_path,
        auth_enabled=False,
        unlock_passphrase="pw",
    )
    app = create_app(settings)
    with TestClient(app) as client:
        app.state.sandbox = _FakeManager("tok", 1)
        with contextlib.suppress(WebSocketDisconnect):
            with client.websocket_connect("/previews/wrong/ws"):
                pass
