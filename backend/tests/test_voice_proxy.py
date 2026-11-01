"""The recording proxy forwards a streamed completion unchanged and keeps only the
tool-bearing request's body."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import StreamingResponse
from starlette.routing import Route

from evals.voice.proxy import RecordingProxy, carries_tools

CHUNKS = [b"data: one\n\n", b"data: two\n\n", b"data: [DONE]\n\n"]


async def _completions(request: Request) -> StreamingResponse:
    async def stream() -> AsyncIterator[bytes]:
        for chunk in CHUNKS:
            yield chunk

    auth = request.headers.get("authorization", "")
    return StreamingResponse(stream(), media_type="text/event-stream", headers={"x-auth": auth})


async def test_the_proxy_forwards_and_captures_once(tmp_path: Path) -> None:
    app = Starlette(routes=[Route("/v1/chat/completions", _completions, methods=["POST"])])
    upstream = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
    serving = asyncio.create_task(upstream.serve())
    while not upstream.started:
        await asyncio.sleep(0.05)
    port = upstream.servers[0].sockets[0].getsockname()[1]

    proxy = RecordingProxy(f"http://127.0.0.1:{port}/v1")
    await proxy.start()
    target = tmp_path / "context" / "banter.json"
    proxy.arm(target)
    title = {"messages": [{"role": "user", "content": "name this"}]}
    turn = {"messages": [{"role": "system", "content": "You are Talos."}], "tools": [{"x": 1}]}
    try:
        async with httpx.AsyncClient(headers={"Authorization": "Bearer secret"}) as client:
            url = f"{proxy.base_url}/chat/completions"
            _ = await client.post(url, json=title)
            assert not target.exists()
            response = await client.post(url, json=turn)
            assert response.content == b"".join(CHUNKS)
            assert response.headers["x-auth"] == "Bearer secret"
            _ = await client.post(url, json={**turn, "tools": [{"y": 2}]})
    finally:
        await proxy.stop()
        upstream.should_exit = True
        await serving

    saved = target.read_text()
    assert json.loads(saved) == turn
    assert "secret" not in saved


def test_only_a_body_with_tools_counts() -> None:
    assert carries_tools(b'{"tools": [{"x": 1}]}')
    assert not carries_tools(b'{"tools": []}')
    assert not carries_tools(b"not json")
