"""Oversized tool results go to the thread's scratch; the model gets a preview and the path.

A result past ``tool_output_spill_chars`` would flood the context if returned whole, and
cutting it silently loses what the model may need. The unit is the string leaf: the result
keeps its type and shape, and each oversized string inside it is saved raw to its own file
under ``tool-output/`` and replaced by a head-and-tail preview. Scratch is mounted at
``~/scratch`` on every thread, project or not, so ``files_read_file`` pages through it.
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import fields, is_dataclass, replace
from typing import cast

from pydantic import BaseModel
from pydantic_ai import RunContext, ToolReturn
from pydantic_ai.messages import ToolCallPart, is_multi_modal_content
from pydantic_core import to_json

from core.config import get_settings
from core.text import truncate_middle
from core.untrusted import PREAMBLE_LINE, wrap_untrusted
from services.home.paths import SCRATCH
from services.sandbox import SandboxSessionManager
from services.workspace import sandbox_workspace

from .code_mode_marks import RUN_CODE, script_call_of
from .deps import RunDeps

logger = logging.getLogger(__name__)

SPILL_DIR = "tool-output"

#: Results the model already reads in pieces.
_NEVER_SPILLED = frozenset({"files_read_file"})

#: Categories whose results are personal data. Scratch is plaintext, so they stay in the
#: context under their own caps.
_PERSONAL = frozenset({"mail", "calendar", "memory", "vault", "corpus", "platform"})

_PREVIEW_CHARS = 8_000
#: What a preview adds around its head and tail: the omission marker and the path note.
_PREVIEW_OVERHEAD = 500
_MAX_DEPTH = 24

_FENCE_OPEN = re.compile(r'<untrusted id="[0-9a-f]+"')
_PREAMBLE = re.compile(PREAMBLE_LINE)
_FENCE = re.compile(
    "".join(
        (
            f"(?:{PREAMBLE_LINE}\\n)?",
            r'<untrusted id="(?P<nonce>[0-9a-f]+)"(?: source="(?P<source>[^"]*)")?>\n',
            r'(?P<body>.*?)\n</untrusted id="(?P=nonce)">',
        )
    ),
    re.DOTALL,
)
_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]")


def _category(name: str) -> str:
    return name.split("_", 1)[0]


def _parts(value: object) -> Iterator[object]:
    """The direct children of a container the spill looks inside, in a fixed order."""
    if type(value) is dict:
        yield from cast(Mapping[object, object], value).values()
    elif type(value) in (list, tuple):
        yield from cast(Sequence[object], value)
    elif is_dataclass(value) and not isinstance(value, type):
        for field in fields(value):
            yield cast(object, getattr(value, field.name))
    elif isinstance(value, BaseModel):
        for field_name in type(value).model_fields:
            yield cast(object, getattr(value, field_name))


def _scan(value: object, leaves: list[str], depth: int = 0) -> bool:
    """Collect the string leaves of ``value`` in order; False when it holds media."""
    if isinstance(value, str):
        leaves.append(value)
        return True
    if isinstance(value, bytes | bytearray) or is_multi_modal_content(value):
        return False
    if depth >= _MAX_DEPTH:
        return True
    return all(_scan(part, leaves, depth + 1) for part in _parts(value))


def _substitute(value: object, swaps: Mapping[int, str], counter: list[int]) -> object:
    """``value`` with the leaves numbered in ``swaps`` replaced, in the shape it had."""
    if isinstance(value, str):
        index = counter[0]
        counter[0] += 1
        return swaps.get(index, value)
    if isinstance(value, bytes | bytearray) or is_multi_modal_content(value):
        return value
    if type(value) is dict:
        return {
            key: _substitute(part, swaps, counter)
            for key, part in cast(Mapping[object, object], value).items()
        }
    if type(value) is list:
        return [_substitute(part, swaps, counter) for part in cast(Sequence[object], value)]
    if type(value) is tuple:
        return tuple(_substitute(part, swaps, counter) for part in cast(Sequence[object], value))
    if is_dataclass(value) and not isinstance(value, type):
        changes = {
            field.name: _substitute(cast(object, getattr(value, field.name)), swaps, counter)
            for field in fields(value)
        }
        init = {field.name for field in fields(value) if field.init}
        return replace(value, **{key: new for key, new in changes.items() if key in init})
    if isinstance(value, BaseModel):
        update = {
            name: _substitute(cast(object, getattr(value, name)), swaps, counter)
            for name in type(value).model_fields
        }
        return value.model_copy(update=update)
    return value


def _size(value: object, leaves: Sequence[str]) -> int:
    """About how many characters ``value`` takes when serialized for the model."""
    if isinstance(value, str):
        return len(value)
    try:
        return len(to_json(value, fallback=repr))
    except ValueError:
        return sum(len(leaf) for leaf in leaves)


def _cut(text: str, budget: int) -> str:
    head, tail, elided = truncate_middle(text, budget)
    if not elided:
        return text
    return f"{head}\n[… {elided:,} characters omitted …]\n{tail}"


def _cut_fenced(text: str, budget: int) -> str:
    """A preview of text holding untrusted fences, each fenced excerpt cut from its inner
    text and wrapped again, so a cut never leaves half a fence and trusted text between
    the fences is never turned into data."""
    spans = list(_FENCE.finditer(text))
    if not spans:
        return wrap_untrusted(_cut(text, budget))
    share = max(budget // (2 * len(spans) + 1), 500)
    pieces: list[str] = []
    position = 0
    for span in spans:
        between = text[position : span.start()]
        if between.strip():
            pieces.append(_cut(between, share))
        pieces.append(wrap_untrusted(_cut(span["body"], share), source=span["source"]))
        position = span.end()
    if text[position:].strip():
        pieces.append(_cut(text[position:], share))
    return "\n".join(pieces)


def _preview(text: str, budget: int, path: str | None) -> str:
    fenced = _FENCE_OPEN.search(text) is not None or _PREAMBLE.search(text) is not None
    shown = _cut_fenced(text, budget) if fenced else _cut(text, budget)
    if path is None:
        note = "[The middle was left out, and the full text could not be saved.]"
    else:
        note = (
            f"[The full text, {len(text):,} characters, is saved at {path}; "
            + "read it in pieces with files_read_file.]"
        )
    return f"{shown}\n{note}"


async def _save(deps: RunDeps, relpath: str, text: str) -> bool:
    sessions = deps.caps.get_optional(SandboxSessionManager)
    if sessions is None:
        return False
    try:
        workspace = await sandbox_workspace(sessions, deps.workspace_key, holder=deps.run)
        await asyncio.to_thread(workspace.files.write_file, relpath, text.encode())
    except Exception:
        logger.warning("could not save an oversized tool result", exc_info=True)
        return False
    return True


def _tool_return(result: object) -> ToolReturn[object] | None:
    return cast("ToolReturn[object]", result) if isinstance(result, ToolReturn) else None


def _script_touched_personal_data(result: ToolReturn[object]) -> bool:
    metadata = cast(object, result.metadata)
    if not isinstance(metadata, dict):
        return False
    calls = cast(Mapping[str, object], metadata).get("tool_calls")
    if not isinstance(calls, dict):
        return False
    return any(
        isinstance(call, ToolCallPart) and _category(call.tool_name) in _PERSONAL
        for call in cast(Mapping[object, object], calls).values()
    )


async def _spill(ctx: RunContext[RunDeps], name: str, result: object) -> object:
    if name in _NEVER_SPILLED or _category(name) in _PERSONAL or script_call_of(ctx) is not None:
        return result
    wrapped = _tool_return(result)
    if wrapped is not None and name == RUN_CODE and _script_touched_personal_data(wrapped):
        return result
    value = cast(object, wrapped.return_value) if wrapped is not None else result
    limit = get_settings().tool_output_spill_chars
    leaves: list[str] = []
    if not _scan(value, leaves):
        return result
    remaining = _size(value, leaves)
    if remaining <= limit:
        return result
    budget = min(_PREVIEW_CHARS, limit)
    kept = budget + _PREVIEW_OVERHEAD
    stem = _UNSAFE_NAME.sub("_", ctx.tool_call_id or "") or "call"
    swaps: dict[int, str] = {}
    for index in sorted(range(len(leaves)), key=lambda i: len(leaves[i]), reverse=True):
        leaf = leaves[index]
        if remaining <= limit or len(leaf) <= kept:
            break
        relpath = f"{SPILL_DIR}/{stem}-{uuid.uuid4().hex[:8]}.txt"
        saved = await _save(ctx.deps, relpath, leaf)
        swaps[index] = _preview(leaf, budget, f"{SCRATCH.path}/{relpath}" if saved else None)
        remaining -= len(leaf) - len(swaps[index])
    if not swaps:
        return result
    spilled = _substitute(value, swaps, [0])
    return replace(wrapped, return_value=spilled) if wrapped is not None else spilled


async def spill_large(ctx: RunContext[RunDeps], name: str, result: object) -> object:
    """``result`` with its oversized string leaves previewed and saved, or as it was.

    The result keeps its type and shape (a ``ToolReturn`` keeps its content and metadata,
    a ``Citable`` its citations), and a failure here never fails the tool that succeeded.
    """
    try:
        return await _spill(ctx, name, result)
    except Exception:
        logger.warning("could not spill the result of %s", name, exc_info=True)
        return result
