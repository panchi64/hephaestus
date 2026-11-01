"""A pass-through proxy between the platform and the model endpoint that keeps the exact
request Talos received, which the platform itself doesn't store.

Every request is forwarded unchanged and its response streamed back. Once armed, the next
request carrying tools (Talos's turn, not a title or summary call) is written to a file:
the body only, never the headers, which hold the endpoint's key.
"""

from __future__ import annotations

import asyncio
import json
import socket
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import StreamingResponse
from starlette.routing import Route

#: Headers that describe one hop, not the message, so they are never passed along.
_HOP = frozenset(
    {"host", "connection", "keep-alive", "transfer-encoding", "content-length", "upgrade"}
)
_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def carries_tools(body: bytes) -> bool:
    try:
        payload = json.loads(body)
    except ValueError:
        return False
    return isinstance(payload, dict) and bool(payload.get("tools"))


class RecordingProxy:
    def __init__(self, upstream: str) -> None:
        parts = urlsplit(upstream)
        self._origin: str = f"{parts.scheme}://{parts.netloc}"
        self._path: str = parts.path.rstrip("/")
        self._armed: Path | None = None
        self._client: httpx.AsyncClient = httpx.AsyncClient(timeout=None)
        self._port: int = _free_port()
        app = Starlette(routes=[Route("/{path:path}", self._forward, methods=_METHODS)])
        config = uvicorn.Config(app, host="127.0.0.1", port=self._port, log_level="warning")
        self._server: uvicorn.Server = uvicorn.Server(config)
        self._task: asyncio.Task[None] | None = None

    @property
    def base_url(self) -> str:
        """The upstream's base URL with the proxy in front: same path, local origin."""
        return f"http://127.0.0.1:{self._port}{self._path}"

    def arm(self, target: Path) -> None:
        self._armed = target

    async def start(self) -> None:
        self._task = asyncio.create_task(self._server.serve())
        while not self._server.started:
            if self._task.done():
                self._task.result()
            await asyncio.sleep(0.05)

    async def stop(self) -> None:
        self._server.should_exit = True
        if self._task is not None and not self._task.done():
            await self._task
        await self._client.aclose()

    async def _forward(self, request: Request) -> StreamingResponse:
        body = await request.body()
        if self._armed is not None and carries_tools(body):
            self._armed.parent.mkdir(parents=True, exist_ok=True)
            _ = self._armed.write_text(json.dumps(json.loads(body), indent=2) + "\n")
            self._armed = None
        query = f"?{request.url.query}" if request.url.query else ""
        upstream = self._client.build_request(
            request.method,
            f"{self._origin}{request.url.path}{query}",
            headers=[(k, v) for k, v in request.headers.items() if k.lower() not in _HOP],
            content=body,
        )
        response = await self._client.send(upstream, stream=True)
        return StreamingResponse(
            response.aiter_raw(),
            status_code=response.status_code,
            headers={k: v for k, v in response.headers.items() if k.lower() not in _HOP},
            background=BackgroundTask(response.aclose),
        )
