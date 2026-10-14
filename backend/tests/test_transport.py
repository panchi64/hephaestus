"""SSE transport: keepalive comments distinguish a quiet run from a dead one, and
a terminal run still replays its full record before the stream ends."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from typing import cast

import pytest

import core.sse as sse
import runs.transport as transport
from runs.events import AnswerDelta
from runs.run import Run
from runs.stream import RunStream

type _Frame = str | bytes | memoryview


def _frames(run: Run) -> AsyncGenerator[_Frame]:
    body = transport.sse_response(run, after_seq=0).body_iterator
    assert isinstance(body, AsyncGenerator)
    return cast(AsyncGenerator[_Frame, None], body)


def _text(frame: _Frame) -> str:
    return frame if isinstance(frame, str) else bytes(frame).decode()


async def test_keepalive_ping_during_quiet_gap(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(sse, "KEEPALIVE_INTERVAL_S", 0.02)
    run = Run(id="r1", kind="chat", owner_id="operator", stream=RunStream())
    _ = run.emit(AnswerDelta(text="hi"))  # one buffered event, then quiet

    agen = _frames(run)
    try:
        first = _text(await asyncio.wait_for(anext(agen), 1.0))
        assert "answer.delta" in first  # the backlog event flushes immediately

        # No further events while the run stays open → a keepalive comment arrives,
        # so a foreground client can tell the connection is alive, not stalled.
        second = _text(await asyncio.wait_for(anext(agen), 1.0))
        assert second.startswith(": ping")
    finally:
        run.stream.close()
        await agen.aclose()


async def test_terminal_run_replays_then_ends(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(sse, "KEEPALIVE_INTERVAL_S", 0.02)
    run = Run(id="r2", kind="chat", owner_id="operator", stream=RunStream())
    _ = run.emit(AnswerDelta(text="done"))
    run.stream.close()

    frames = [_text(f) async for f in _frames(run)]
    # The full backlog replays and the generator ends — no endless ping loop.
    assert any("answer.delta" in f for f in frames)
