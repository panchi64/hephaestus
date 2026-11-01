"""The recording proxy forwards a streamed completion unchanged, keeps only the
tool-bearing request's body, and tallies Anthropic usage under the request's bucket."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import cast

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import StreamingResponse
from starlette.routing import Route

from evals.voice.proxy import RecordingProxy, carries_tools

CHUNKS = [b"data: one\n\n", b"data: two\n\n", b"data: [DONE]\n\n"]


@asynccontextmanager
async def _proxied(
    app: Starlette, path: str, tpm: int | None = None
) -> AsyncGenerator[RecordingProxy]:
    """``app`` served on a free port, with a started proxy in front of ``path`` on it."""
    upstream = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
    serving = asyncio.create_task(upstream.serve())
    while not upstream.started:
        await asyncio.sleep(0.05)
    _host, port = cast(tuple[str, int], upstream.servers[0].sockets[0].getsockname())
    proxy = RecordingProxy(f"http://127.0.0.1:{port}{path}", tokens_per_minute=tpm)
    await proxy.start()
    try:
        yield proxy
    finally:
        await proxy.stop()
        upstream.should_exit = True
        await serving


async def _completions(request: Request) -> StreamingResponse:
    async def stream() -> AsyncIterator[bytes]:
        for chunk in CHUNKS:
            yield chunk

    auth = request.headers.get("authorization", "")
    return StreamingResponse(stream(), media_type="text/event-stream", headers={"x-auth": auth})


async def test_the_proxy_forwards_and_captures_once(tmp_path: Path) -> None:
    app = Starlette(routes=[Route("/v1/chat/completions", _completions, methods=["POST"])])
    target = tmp_path / "context" / "banter.json"
    title = {"messages": [{"role": "user", "content": "name this"}]}
    turn = {"messages": [{"role": "system", "content": "You are Talos."}], "tools": [{"x": 1}]}
    async with (
        _proxied(app, "/v1") as proxy,
        httpx.AsyncClient(headers={"Authorization": "Bearer secret"}) as client,
    ):
        proxy.arm(target)
        url = f"{proxy.base_url}/chat/completions"
        _ = await client.post(url, json=title)
        assert not target.exists()
        response = await client.post(url, json=turn)
        assert response.content == b"".join(CHUNKS)
        assert response.headers["x-auth"] == "Bearer secret"
        _ = await client.post(url, json={**turn, "tools": [{"y": 2}]})

    saved = target.read_text()
    assert json.loads(saved) == turn
    assert "secret" not in saved


START: dict[str, object] = {
    "type": "message_start",
    "message": {
        "id": "m1",
        "type": "message",
        "role": "assistant",
        "content": [],
        "model": "claude-sonnet-5-5",
        "stop_reason": None,
        "stop_sequence": None,
        "usage": {"input_tokens": 40, "output_tokens": 1},
    },
}


async def test_the_proxy_tallies_usage_under_the_request_bucket() -> None:
    seen: list[str | None] = []

    async def messages(request: Request) -> StreamingResponse:
        seen.append(request.headers.get("accept-encoding"))

        async def stream() -> AsyncIterator[bytes]:
            yield f"event: message_start\ndata: {json.dumps(START)}\n\n".encode()

        return StreamingResponse(stream(), media_type="text/event-stream")

    app = Starlette(routes=[Route("/v1/messages", messages, methods=["POST"])])
    async with _proxied(app, "", tpm=1_000) as proxy, httpx.AsyncClient() as client:
        proxy.attribute("banter#1")
        url = f"{proxy.base_url}/v1/messages"
        _ = await client.post(url, json={"tools": [{"x": 1}]})
        _ = await client.post(url, json={"messages": []})

    tally = proxy.take("banter#1")
    assert (tally["agent"].requests, tally["utility"].requests) == (1, 1)
    assert tally["agent"].input_tokens == 40
    assert proxy.take("banter#1") == {}
    assert seen == ["identity", "identity"]
    assert proxy.window.peak == 80


def test_only_a_body_with_tools_counts() -> None:
    assert carries_tools(b'{"tools": [{"x": 1}]}')
    assert not carries_tools(b'{"tools": []}')
    assert not carries_tools(b"not json")
