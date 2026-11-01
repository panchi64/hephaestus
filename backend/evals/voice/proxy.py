"""A pass-through proxy between the platform and the model endpoint that keeps the exact
request Talos received, which the platform itself doesn't store.

Every request is forwarded unchanged and its response streamed back. Once armed, the next
request carrying tools (Talos's turn, not a title or summary call) is written to a file:
the body only, never the headers, which hold the endpoint's key.

Each response's token use is tallied under the bucket set when its request arrived (see
:mod:`.usage`), and with a tokens-per-minute limit, a request waits while the last
minute's prompt tokens are at it.
"""

from __future__ import annotations

import asyncio
import json
import socket
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit

import httpx
import uvicorn
from pydantic import BaseModel, Field, ValidationError
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import StreamingResponse
from starlette.routing import Route

from .record import redact_home
from .usage import Kind, ResponseUsage, TokenWindow, Usage

#: Headers that describe one hop, not the message, so they are never passed along.
_HOP = frozenset(
    {"host", "connection", "keep-alive", "transfer-encoding", "content-length", "upgrade"}
)
#: Replaced by ``identity`` on requests, so the upstream answers uncompressed and the
#: usage events are readable.
_REQUEST_DROP = _HOP | {"accept-encoding"}
_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return cast(tuple[str, int], sock.getsockname())[1]


class _Request(BaseModel):
    tools: list[object] = Field(default_factory=list)


def carries_tools(body: bytes) -> bool:
    try:
        return bool(_Request.model_validate_json(body).tools)
    except ValidationError:
        return False


class RecordingProxy:
    def __init__(self, upstream: str, *, tokens_per_minute: int | None = None) -> None:
        parts = urlsplit(upstream)
        self._origin: str = f"{parts.scheme}://{parts.netloc}"
        self._path: str = parts.path.rstrip("/")
        self._armed: Path | None = None
        self._bucket: str = ""
        self._usage: dict[str, dict[Kind, Usage]] = {}
        self.window: TokenWindow = TokenWindow(tokens_per_minute or sys.maxsize)
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

    def attribute(self, bucket: str) -> None:
        """Tally the usage of requests from now on under ``bucket``."""
        self._bucket = bucket

    def take(self, bucket: str) -> dict[Kind, Usage]:
        """``bucket``'s tally so far, removed from the proxy."""
        return self._usage.pop(bucket, {})

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
        tools = carries_tools(body)
        if self._armed is not None and tools:
            self._armed.parent.mkdir(parents=True, exist_ok=True)
            text = json.dumps(json.loads(body), indent=2) + "\n"
            _ = self._armed.write_text(redact_home(text))
            self._armed = None
        bucket = self._bucket
        kind: Kind = "agent" if tools else "utility"
        while (delay := self.window.wait_s()) > 0:
            print(f"    rate guard: waiting {delay:.0f}s", flush=True)
            await asyncio.sleep(delay)
        query = f"?{request.url.query}" if request.url.query else ""
        upstream = self._client.build_request(
            request.method,
            f"{self._origin}{request.url.path}{query}",
            headers=[
                *((k, v) for k, v in request.headers.items() if k.lower() not in _REQUEST_DROP),
                ("accept-encoding", "identity"),
            ],
            content=body,
        )
        response = await self._client.send(upstream, stream=True)
        reader = ResponseUsage(
            self._origin,
            stream="text/event-stream" in response.headers.get("content-type", ""),
            on_start=self.window.add,
        )

        async def relay() -> AsyncIterator[bytes]:
            try:
                async for chunk in response.aiter_raw():
                    reader.feed(chunk)
                    yield chunk
            finally:
                if (read := reader.finish()) is not None:
                    self._usage.setdefault(bucket, {}).setdefault(kind, Usage()).add(*read)

        return StreamingResponse(
            relay(),
            status_code=response.status_code,
            headers={k: v for k, v in response.headers.items() if k.lower() not in _HOP},
            background=BackgroundTask(response.aclose),
        )
