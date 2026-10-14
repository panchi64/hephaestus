"""The live stream and a reload must tell one turn in the same order.

A turn reaches the transcript twice: as events while it runs, and as a persisted message
once the conversation is read back. The two readers once disagreed — the reload flattened
the turn into one lane per kind — so a work log the operator watched interleave regrouped
itself the moment the run ended.

This test runs one interleaved turn through the real route and records both halves, side
by side, in a fixture the frontend owns: `streamParity.test.ts` folds the events and
decodes the message, and fails if the two produce different blocks. Neither side can
drift alone — a backend change that alters either half fails *here* until the fixture is
regenerated, and the regenerated fixture is then what the frontend is held to.

Regenerate with ``UPDATE_STREAM_PARITY=1 uv run pytest tests/test_stream_parity.py``.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from pydantic import JsonValue
from pydantic_ai.messages import ModelMessage, ModelRequest, ToolReturnPart
from pydantic_ai.models.function import (
    AgentInfo,
    DeltaThinkingCalls,
    DeltaThinkingPart,
    DeltaToolCall,
    DeltaToolCalls,
    FunctionModel,
)

from services.registry import ModelRegistry, ResolvedModel

from ._helpers import client_app, collect_sse_events, register_stub_provider, stub_resolution
from ._json import arr, json_body, obj, s

FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "frontend/src/features/chat/__fixtures__/stream-parity.json"
)

#: The events that build a turn's blocks *and* have a persisted counterpart. Injected
#: context and reviews are live-only by design, so they are not part of the parity claim.
_BLOCK_EVENTS = {"thinking.delta", "answer.delta", "tool.started", "tool.completed", "tool.failed"}
_EVENT_KEYS = ("type", "text", "tool_call_id", "name", "args", "result", "error")


def _returns(messages: list[ModelMessage]) -> int:
    return sum(
        isinstance(part, ToolReturnPart)
        for message in messages
        if isinstance(message, ModelRequest)
        for part in message.parts
    )


async def _interleaved(
    messages: list[ModelMessage], _info: AgentInfo
) -> AsyncIterator[str | DeltaToolCalls | DeltaThinkingCalls]:
    """think → text → call, then think → call, then the answer — every kind change the
    live fold turns into a new block."""
    done = _returns(messages)
    if done == 0:
        yield {0: DeltaThinkingPart(content="Let me ")}
        yield {0: DeltaThinkingPart(content="plan.")}
        yield "Checking the clock."
        yield {1: DeltaToolCall(name="builtin_now", json_args="{}", tool_call_id="c1")}
    elif done == 1:
        yield {0: DeltaThinkingPart(content="Once more.")}
        yield {1: DeltaToolCall(name="builtin_now", json_args="{}", tool_call_id="c2")}
    else:
        yield "Done."


def _event(ev: dict[str, JsonValue], seq: int) -> dict[str, JsonValue]:
    out = {k: ev[k] for k in _EVENT_KEYS if k in ev}
    # The clock's reading changes every run; what it said is not what parity is about.
    if "result" in out:
        out["result"] = "<result>"
    return {**out, "seq": seq}


def _message(msg: dict[str, JsonValue]) -> dict[str, JsonValue]:
    tools: list[JsonValue] = [
        {**obj(t), "result": "<result>" if obj(t).get("result") is not None else None}
        for t in arr(msg["tools"])
    ]
    return {
        "id": "a1",
        "role": msg["role"],
        "content": msg["content"],
        "tools": tools,
        "versions": msg["versions"],
        "segments": msg["segments"],
    }


async def test_the_stream_and_the_reload_record_the_same_turn(monkeypatch: pytest.MonkeyPatch):
    async def resolve_detailed(self: ModelRegistry, _role: str, **_kwargs: object) -> ResolvedModel:
        return await stub_resolution(self, FunctionModel(stream_function=_interleaved))

    register_stub_provider(monkeypatch)
    monkeypatch.setattr(ModelRegistry, "resolve_detailed", resolve_detailed)

    async with client_app() as (client, _app):
        started = json_body(await client.post("/chat", json={"prompt": "what time is it?"}))
        events = await collect_sse_events(client, s(started["run_id"]))
        detail = json_body(await client.get(f"/conversations/{s(started['conversation_id'])}"))

    live = [ev for ev in events if ev["type"] in _BLOCK_EVENTS]
    fixture = {
        "events": [_event(ev, i + 1) for i, ev in enumerate(live)],
        "message": _message(obj(arr(detail["messages"])[-1])),
    }
    rendered = json.dumps(fixture, indent=2, ensure_ascii=False) + "\n"

    if os.environ.get("UPDATE_STREAM_PARITY"):
        FIXTURE.parent.mkdir(parents=True, exist_ok=True)
        _ = FIXTURE.write_text(rendered)
    assert FIXTURE.exists() and FIXTURE.read_text() == rendered, (
        "The live stream or the reload changed shape. Regenerate the fixture with "
        "UPDATE_STREAM_PARITY=1 and run the frontend's streamParity.test.ts against it."
    )
