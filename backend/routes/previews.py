"""Live-preview reverse proxy — the token-gated window into a dev server in a VM.

The agent runs a dev server in its conversation's VM (``services/vm/previews.py``); this
forwards the operator's browser to it. The address carries an unguessable per-preview **token**
(``/previews/{token}/…``) which *is* the credential, and the token rides every
relative subresource load automatically. So this subtree is exempt from the cookie
gate (see ``core/auth``) — it only ever proxies to a loopback preview server,
never to operator data. It is *not* exempt from the vault-locked gate: locking takes
the preview offline with everything else.

The opaque origin the frontend's sandboxed iframe provides is also enforced here, as a
``Content-Security-Policy: sandbox`` without ``allow-same-origin`` on every proxied
response — so agent-authored HTML is isolated from the API origin however it is opened,
not only when the frontend happens to frame it correctly.

Both legs are byte-transparent: HTTP via a streaming ``httpx`` proxy, WebSocket via
a ``websockets`` bridge (so Vite/HMR live-reload works). The operator's credentials
are never forwarded inward, and the preview can't set cookies on the API origin.
"""

from __future__ import annotations

import asyncio

import httpx
from fastapi import APIRouter, HTTPException, Request, Response, WebSocket
from starlette.responses import StreamingResponse
from starlette.websockets import WebSocketDisconnect
from websockets.asyncio.client import ClientConnection
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake

from core.shutdown import ends_on_shutdown, wait_shutdown
from routes import deps
from services.vm import PreviewHandle

router = APIRouter(prefix="/previews", tags=["previews"])

_HTTP_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]

# Connection-scoped headers a proxy must not forward (RFC 7230 §6.1).
_HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)
# Inbound: never leak the operator's credentials to the model's server.
_STRIP_REQUEST = _HOP_BY_HOP | {"cookie", "authorization", "host"}
# Outbound: the preview must not plant cookies on the API origin, and a server
# default of `x-frame-options: deny` must not block the iframe this feature renders
# into (the sandboxed iframe is the isolation boundary, not a framing header). The
# upstream's own CSP is dropped so it can't weaken or complicate ours below.
_STRIP_RESPONSE = _HOP_BY_HOP | {
    "set-cookie",
    "x-frame-options",
    "content-security-policy",
    "content-security-policy-report-only",
}

# The opaque origin, enforced here rather than assumed of the caller. This subtree is
# served from the API's own origin, so agent-authored HTML reaching it top-level (a new
# tab, a link out of the iframe, anything that isn't the frontend's sandboxed iframe)
# would otherwise be same-origin with the API and able to read `/conversations` with the
# operator's session. `sandbox` *without* `allow-same-origin` puts the document in an
# opaque origin — no cookies, no credentialed same-origin fetch — while leaving a dev
# server fully functional. `routes/views.py` makes the same call for the same reason; the
# frontend's iframe attributes are UX, not the enforcement point.
_PREVIEW_SANDBOX = "sandbox allow-scripts allow-forms allow-popups allow-modals allow-downloads"


def _preview(request_or_ws: Request | WebSocket, token: str) -> PreviewHandle | None:
    previews = deps.previews(request_or_ws)
    return previews.resolve(token) if previews is not None else None


def _rewrite_location(value: str, prefix: str) -> str:
    """Pull a redirect that points at the upstream (or site root) back under the
    token prefix, so the browser stays inside the preview."""
    for scheme in ("http://", "https://"):
        if value.startswith(scheme):
            rest = value[len(scheme) :]
            slash = rest.find("/")
            return prefix + (rest[slash:] if slash != -1 else "/")
    if value.startswith("/"):
        return prefix + value
    return value


def _response_headers(headers: httpx.Headers, prefix: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in headers.items():
        lower = key.lower()
        if lower in _STRIP_RESPONSE:
            continue
        out[key] = _rewrite_location(value, prefix) if lower == "location" else value
    out["x-content-type-options"] = "nosniff"
    out["content-security-policy"] = _PREVIEW_SANDBOX
    return out


@router.api_route("/{token}/{path:path}", methods=_HTTP_METHODS)
async def proxy_http(token: str, path: str, request: Request) -> Response:
    handle = _preview(request, token)
    if handle is None:
        raise HTTPException(status_code=404, detail="preview not found")

    prefix = f"/previews/{token}"
    client: httpx.AsyncClient = deps.preview_client(request)
    url = httpx.URL(
        scheme="http",
        host="127.0.0.1",
        port=handle.host_port,
        path="/" + path,
        query=request.url.query.encode("ascii"),
    )
    headers = [
        (k, v) for k, v in request.headers.raw if k.decode("latin-1").lower() not in _STRIP_REQUEST
    ]
    headers.append((b"x-forwarded-prefix", prefix.encode("ascii")))
    upstream = client.build_request(request.method, url, headers=headers, content=request.stream())
    try:
        response = await client.send(upstream, stream=True)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"preview server error: {exc}") from exc

    async def _body():
        # The finally returns the upstream connection to the pool even when the
        # operator's browser disconnects mid-stream — Starlette aclose()s this
        # generator on disconnect, whereas a background task only runs on a clean
        # finish, so without it a navigated-away stream would leak a connection.
        try:
            async for chunk in ends_on_shutdown(response.aiter_raw()):
                yield chunk
        finally:
            await response.aclose()

    return StreamingResponse(
        _body(),
        status_code=response.status_code,
        headers=_response_headers(response.headers, prefix),
    )


@router.websocket("/{token}/{path:path}")
async def proxy_ws(websocket: WebSocket, token: str, path: str) -> None:
    handle = _preview(websocket, token)
    if handle is None:
        await websocket.close(code=4404)
        return

    query = websocket.url.query
    upstream_url = f"ws://127.0.0.1:{handle.host_port}/{path}" + (f"?{query}" if query else "")
    subprotocols = websocket.scope.get("subprotocols") or None
    try:
        upstream = await ws_connect(upstream_url, subprotocols=subprotocols, open_timeout=10)
    except OSError, ConnectionClosed, TimeoutError, InvalidHandshake:
        await websocket.close(code=1011)
        return

    async with upstream:
        await websocket.accept(subprotocol=upstream.subprotocol)
        await _bridge(websocket, upstream)


async def _bridge(client_ws: WebSocket, upstream: ClientConnection) -> None:
    """Pump frames both ways until either side closes."""

    async def client_to_upstream() -> None:
        try:
            while True:
                message = await client_ws.receive()
                if message["type"] == "websocket.disconnect":
                    return
                text: object = message.get("text")
                data: object = message.get("bytes")
                if isinstance(text, str):
                    await upstream.send(text)
                elif isinstance(data, bytes):
                    await upstream.send(data)
        except WebSocketDisconnect, ConnectionClosed:
            return

    async def upstream_to_client() -> None:
        try:
            async for message in upstream:
                if isinstance(message, str):
                    await client_ws.send_text(message)
                else:
                    await client_ws.send_bytes(message)
        except WebSocketDisconnect, ConnectionClosed:
            return

    tasks = [
        asyncio.create_task(client_to_upstream()),
        asyncio.create_task(upstream_to_client()),
        # A hot-reload socket never closes on its own; shutdown is what ends it.
        asyncio.create_task(wait_shutdown()),
    ]
    _done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        _ = task.cancel()
    # Await the cancelled side so its teardown finishes and no exception is orphaned.
    _ = await asyncio.gather(*pending, return_exceptions=True)
