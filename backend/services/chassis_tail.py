"""The chassis tail: items the chassis appends to the operator's own prompt content.

The stop notice, the task list, document state and a command's expansion persist as
sent, so the replayed history is the prefix the provider cached. Every reader of the
operator's words slices them off. The record lives in the request's metadata as
``[part_index, count]`` pairs, and every merge or slice of a request goes through here so
the indexes keep pointing at the right part.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace

from pydantic_ai import ModelRequest, UserPromptPart
from pydantic_ai.messages import ModelMessage, UserContent

from services.shapes import as_dict, as_int, as_list

#: Request-metadata key holding the ``[part_index, count]`` pairs.
CHASSIS_TAIL_KEY = "chassis_tail"


def tail_stamp(parts: Sequence[object], count: int) -> dict[str, object]:
    """The metadata marking the last ``count`` items of the first user part as chassis."""
    index = next((i for i, p in enumerate(parts) if isinstance(p, UserPromptPart)), None)
    if index is None or count <= 0:
        return {}
    return {CHASSIS_TAIL_KEY: [[index, count]]}


def chassis_tails(message: ModelRequest) -> dict[int, int]:
    """Part index to how many of that part's trailing items the chassis added."""
    pairs = as_list((as_dict(message.metadata) or {}).get(CHASSIS_TAIL_KEY)) or []
    tails: dict[int, int] = {}
    for pair in pairs:
        items = as_list(pair) or []
        if len(items) != 2:
            continue
        index, count = as_int(items[0]), as_int(items[1])
        if index is not None and count is not None and count > 0:
            tails[index] = count
    return tails


def with_tails(message: ModelRequest, tails: Mapping[int, int]) -> ModelRequest:
    """``message`` with its tail record set to ``tails``."""
    held = as_dict(message.metadata) or {}
    metadata = {k: v for k, v in held.items() if k != CHASSIS_TAIL_KEY}
    kept = sorted((i, n) for i, n in tails.items() if n > 0)
    if kept:
        metadata[CHASSIS_TAIL_KEY] = [[i, n] for i, n in kept]
    return replace(message, metadata=metadata or None)


def operator_content(message: ModelRequest, part: UserPromptPart) -> str | Sequence[UserContent]:
    """``part``'s content without the items the chassis appended to the operator's own."""
    content = part.content
    if isinstance(content, str):
        return content
    index = next((i for i, p in enumerate(message.parts) if p is part), None)
    tail = chassis_tails(message).get(index) if index is not None else None
    if not tail:
        return content
    return list(content[: len(content) - tail])


def part_content(message: ModelMessage, part: object) -> object:
    """A part's content as a reader of the operator's words should see it."""
    if isinstance(message, ModelRequest) and isinstance(part, UserPromptPart):
        return operator_content(message, part)
    return getattr(part, "content", None)


def merge_requests(first: ModelRequest, second: ModelRequest) -> ModelRequest:
    """``second``'s parts appended to ``first``'s, with both tail records carried over."""
    offset = len(first.parts)
    tails = {
        **chassis_tails(first),
        **{offset + i: n for i, n in chassis_tails(second).items()},
    }
    merged = replace(
        first,
        parts=[*first.parts, *second.parts],
        metadata={**(first.metadata or {}), **(second.metadata or {})} or None,
    )
    return with_tails(merged, tails)


def parts_from(message: ModelRequest, start: int) -> ModelRequest:
    """``message`` without its first ``start`` parts, its tail record re-indexed to match."""
    tails = {i - start: n for i, n in chassis_tails(message).items() if i >= start}
    return with_tails(replace(message, parts=message.parts[start:]), tails)


def prompt_index(message: ModelRequest) -> int | None:
    """Where the operator's prompt sits in a trailing request: the user part carrying a
    tail, else the last one. A merged request opens on a compaction checkpoint, and the
    prompt is what the merge appended."""
    tails = chassis_tails(message)
    users = [i for i, p in enumerate(message.parts) if isinstance(p, UserPromptPart)]
    tailed = [i for i in users if i in tails]
    return tailed[-1] if tailed else (users[-1] if users else None)


def with_tail(message: ModelRequest, texts: Sequence[str]) -> ModelRequest:
    """``message`` with the prompt's (:func:`prompt_index`) chassis tail replaced by ``texts``."""
    tails = chassis_tails(message)
    index = prompt_index(message)
    if index is None or (not texts and index not in tails):
        return message
    part = message.parts[index]
    if not isinstance(part, UserPromptPart):
        return message
    own = operator_content(message, part)
    content = [own] if isinstance(own, str) else list(own)
    parts = list(message.parts)
    parts[index] = replace(part, content=[*content, *texts])
    return with_tails(replace(message, parts=parts), {**tails, index: len(texts)})
