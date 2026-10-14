"""SSE transport: keepalive comments distinguish a quiet run from a dead one, and
a terminal run still replays its full record before the stream ends."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from typing import Any

import core.sse as sse
import runs.transport as transport
from runs.events import AnswerDelta
from runs.run import Run
from runs.stream import RunStream


def _frames(run: Run) -> AsyncGenerator[Any]:
    body = transport.sse_response(run, after_seq=0).body_iterator
    assert isinstance(body, AsyncGenerator)
    return body


async def test_keepalive_ping_during_quiet_gap(monkeypatch):
    monkeypatch.setattr(sse, "KEEPALIVE_INTERVAL_S", 0.02)
    run = Run(id="r1", kind="chat", owner_id="operator", stream=RunStream())
    run.emit(AnswerDelta(text="hi"))  # one buffered event, then quiet

    agen = _frames(run)
    try:
        first = await asyncio.wait_for(anext(agen), 1.0)
        assert "answer.delta" in first  # the backlog event flushes immediately

        # No further events while the run stays open → a keepalive comment arrives,
        # so a foreground client can tell the connection is alive, not stalled.
        second = await asyncio.wait_for(anext(agen), 1.0)
        assert second.startswith(": ping")
    finally:
        run.stream.close()
        await agen.aclose()


async def test_terminal_run_replays_then_ends(monkeypatch):
    monkeypatch.setattr(sse, "KEEPALIVE_INTERVAL_S", 0.02)
    run = Run(id="r2", kind="chat", owner_id="operator", stream=RunStream())
    run.emit(AnswerDelta(text="done"))
    run.stream.close()

    frames = [f async for f in _frames(run)]
    # The full backlog replays and the generator ends — no endless ping loop.
    assert any("answer.delta" in f for f in frames)
