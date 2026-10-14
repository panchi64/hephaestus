"""Encoding a reply in the OpenAI chat-completions wire format, both shapes.

Both shapes in one module because they have to agree. The same scripted reply is
delivered either as a single body or as a sequence of deltas, and a client that gets a
tool call one way and not the other is being told two different things by one fixture —
which is the hardest kind of test failure to read, because the fixture is the part
nobody suspects.

The streaming shape is the fiddly one, and the fiddliness is all in tool calls: the name
and id arrive once, in the first delta for that index, and the arguments arrive as string
fragments to be concatenated. A stub that sent whole arguments in one delta would still
work against a client that concatenates, and would therefore never exercise the path
where a real server splits them — so this splits them deliberately.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from dataclasses import dataclass, field

#: How many characters of tool-call arguments go in one delta. Small enough that any
#: realistic call is split across several, because a single-fragment stream is exactly
#: the case that hides a reassembly bug.
_ARG_CHUNK = 12

#: How many characters of content go in one delta.
_TEXT_CHUNK = 24


@dataclass(frozen=True, slots=True)
class ToolCall:
    """One tool call the scripted reply asks for."""

    name: str
    #: The arguments as JSON *text*, which is what the wire carries — a dict here would
    #: hide the fragmenting this module exists to exercise.
    arguments: str = "{}"
    id: str = "call_stub"


@dataclass(frozen=True, slots=True)
class Reply:
    """What the stub decided to say: some text, some tool calls, or both."""

    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)

    @property
    def finish_reason(self) -> str:
        return "tool_calls" if self.tool_calls else "stop"


def _usage(reply: Reply) -> dict[str, int]:
    """Plausible rather than accurate. Nothing under test measures tokens against a real
    tokenizer, but a zero would make the context estimator's arithmetic degenerate."""
    completion = max(1, len(reply.text) // 4 + sum(len(c.arguments) // 4 for c in reply.tool_calls))
    return {"prompt_tokens": 16, "completion_tokens": completion, "total_tokens": 16 + completion}


def _unique_ids(reply: Reply) -> list[str]:
    """One id per tool call, unique among themselves.

    Only a *duplicate* id is rewritten. Ids are how a client matches each result back to
    the call that asked for it, so two calls sharing one would be indistinguishable — but
    an id the caller set deliberately, to assert on, must survive being one of several.
    """
    seen: set[str] = set()
    ids: list[str] = []
    for index, call in enumerate(reply.tool_calls):
        identifier = call.id if call.id not in seen else f"{call.id}_{index}"
        seen.add(identifier)
        ids.append(identifier)
    return ids


def _tool_calls_body(reply: Reply) -> list[dict[str, object]]:
    """The wire form of a reply's tool calls."""
    return [
        {
            "id": identifier,
            "type": "function",
            "function": {"name": call.name, "arguments": call.arguments},
        }
        for identifier, call in zip(_unique_ids(reply), reply.tool_calls, strict=True)
    ]


def completion(model: str, reply: Reply, *, request_id: str) -> dict[str, object]:
    """The non-streaming response body."""
    message: dict[str, object] = {"role": "assistant", "content": reply.text or None}
    if reply.tool_calls:
        message["tool_calls"] = _tool_calls_body(reply)
    return {
        "id": request_id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": reply.finish_reason}],
        "usage": _usage(reply),
    }


def _chunk(model: str, request_id: str, **choice: object) -> str:
    body = {
        "id": request_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, **choice}],
    }
    return f"data: {json.dumps(body)}\n\n"


def _split(text: str, size: int) -> list[str]:
    return [text[at : at + size] for at in range(0, len(text), size)] or [""]


def stream(model: str, reply: Reply, *, request_id: str, include_usage: bool) -> Iterator[str]:
    """The streaming response, as ready-to-write SSE frames.

    ``include_usage`` follows the client's ``stream_options``. When asked for, the usage
    arrives in a final frame carrying **no choices** — which is the part worth getting
    right, since a client that reads ``choices[0]`` unconditionally would crash on it,
    and a stub that never sent one would never reveal that.
    """
    yield _chunk(model, request_id, delta={"role": "assistant"}, finish_reason=None)

    if reply.text:
        for piece in _split(reply.text, _TEXT_CHUNK):
            yield _chunk(model, request_id, delta={"content": piece}, finish_reason=None)

    for index, (identifier, call) in enumerate(
        zip(_unique_ids(reply), reply.tool_calls, strict=True)
    ):
        # The opening delta carries the identity and an empty argument string; every
        # later one carries only a fragment, indexed so the client knows what to append
        # it to. This is the shape a real server produces.
        yield _chunk(
            model,
            request_id,
            delta={
                "tool_calls": [
                    {
                        "index": index,
                        "id": identifier,
                        "type": "function",
                        "function": {"name": call.name, "arguments": ""},
                    }
                ]
            },
            finish_reason=None,
        )
        for piece in _split(call.arguments, _ARG_CHUNK):
            yield _chunk(
                model,
                request_id,
                delta={"tool_calls": [{"index": index, "function": {"arguments": piece}}]},
                finish_reason=None,
            )

    yield _chunk(model, request_id, delta={}, finish_reason=reply.finish_reason)
    if include_usage:
        body: dict[str, object] = {
            "id": request_id,
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [],
            "usage": _usage(reply),
        }
        yield f"data: {json.dumps(body)}\n\n"
    yield "data: [DONE]\n\n"
