"""Oversized tool results go to the thread's scratch; the model gets a preview and the path.

A result past ``tool_output_spill_chars`` would flood the context if returned whole, and
cutting it silently loses what the model may need. The unit is the string leaf: the result
keeps its type and shape, and each oversized string inside it is saved raw to its own file
under ``tool-output/`` and replaced by a head-and-tail preview. When what is left is still
too big, the whole value is saved as one JSON file instead and shape is lost. Scratch is
mounted at ``~/scratch`` on every thread, project or not, so ``files_read_file`` pages
through it; a file of untrusted text is read back fenced (``tools/files.py``).
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

from core.citations import Citable
from core.config import get_settings
from core.declarations import DeclaredSet
from core.text import elide_middle
from core.untrusted import PREAMBLE_LINE, wrap_untrusted
from services.home.paths import SCRATCH
from services.home.scratch import ScratchDirs
from services.tool_policy import permission_disabled_tools
from services.workspace import sandbox_workspace

from .code_mode_marks import RUN_CODE, script_call_of
from .deps import RunDeps

logger = logging.getLogger(__name__)

SPILL_DIR = "tool-output"
#: A file of text that came from an untrusted source, fence removed. Reading one back
#: fences every page.
UNTRUSTED_SUFFIX = ".untrusted.txt"
READ_BACK_TOOL = "files_read_file"

#: Results the model already reads in pieces.
_NEVER_SPILLED = frozenset({READ_BACK_TOOL})

#: Categories whose results may carry personal data. Scratch is plaintext, so they are never
#: saved to it; they stay in the context under their own caps, and ``external`` is cut.
#: Features add theirs on the manifest (``FeatureManifest.personal_categories``).
PERSONAL_CATEGORIES: DeclaredSet[str] = DeclaredSet(
    frozenset({"calendar", "memory", "vault", "corpus", "platform", "agents", "browse", "external"})
)
_EXTERNAL = "external"
_NOT_SAVED_NOTE = (
    "[The middle was left out. The full result was not saved because it may hold personal data.]"
)

_PREVIEW_CHARS = 8_000
#: The most a preview adds to its head and tail: the omission marker, the path note and the
#: fence around untrusted text.
_PREVIEW_OVERHEAD = 700
_MAX_DEPTH = 24
_STEM_CHARS = 64
_SAVE_TIMEOUT_S = 10.0

_FENCE_OPEN = re.compile(r'<untrusted id="')
_FENCE_CLOSE = '</untrusted id="'
_PREAMBLE = re.compile(PREAMBLE_LINE)
_MARKUP = re.compile(
    "|".join(
        (
            f"(?:{PREAMBLE_LINE}\\n?)",
            r'<untrusted id="[0-9a-f]+"(?: source="[^"]*")?>\n?',
            r'\n?</untrusted id="[0-9a-f]+">',
        )
    )
)
_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]")


def _category(name: str) -> str:
    return name.split("_", 1)[0]


def is_untrusted_spill(rel: str) -> bool:
    """Whether ``rel``, a path inside the scratch, is a saved untrusted result."""
    folder, _, name = rel.rpartition("/")
    return folder == SPILL_DIR and name.endswith(UNTRUSTED_SUFFIX)


def _is_fenced(text: str) -> bool:
    return (
        _FENCE_OPEN.search(text) is not None
        or _FENCE_CLOSE in text
        or _PREAMBLE.search(text) is not None
    )


def _attr(value: object, name: str) -> object:
    return cast(object, getattr(value, name))


def _parts(value: object) -> Iterator[object]:
    """The direct children of a container the spill looks inside, in a fixed order."""
    if type(value) is dict:
        yield from cast(Mapping[object, object], value).values()
    elif type(value) in (list, tuple):
        yield from cast(Sequence[object], value)
    elif is_dataclass(value) and not isinstance(value, type):
        for field in fields(value):
            yield _attr(value, field.name)
    elif isinstance(value, BaseModel):
        for field_name in type(value).model_fields:
            yield _attr(value, field_name)


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


def _substitute(
    value: object, swaps: Mapping[int, str], counter: list[int], depth: int = 0
) -> object:
    """``value`` with the leaves numbered in ``swaps`` replaced, in the shape it had.

    Visits exactly the leaves ``_scan`` collected, in the same order.
    """
    if isinstance(value, str):
        index = counter[0]
        counter[0] += 1
        return swaps.get(index, value)
    if depth >= _MAX_DEPTH or isinstance(value, bytes | bytearray) or is_multi_modal_content(value):
        return value
    deeper = depth + 1
    if type(value) is dict:
        return {
            key: _substitute(part, swaps, counter, deeper)
            for key, part in cast(Mapping[object, object], value).items()
        }
    if type(value) is list:
        return [_substitute(part, swaps, counter, deeper) for part in cast(Sequence[object], value)]
    if type(value) is tuple:
        return tuple(
            _substitute(part, swaps, counter, deeper) for part in cast(Sequence[object], value)
        )
    if is_dataclass(value) and not isinstance(value, type):
        changes = {
            field.name: _substitute(_attr(value, field.name), swaps, counter, deeper)
            for field in fields(value)
        }
        init = {field.name for field in fields(value) if field.init}
        return replace(value, **{key: new for key, new in changes.items() if key in init})
    if isinstance(value, BaseModel):
        update = {
            name: _substitute(_attr(value, name), swaps, counter, deeper)
            for name in type(value).model_fields
        }
        return value.model_copy(update=update)
    return value


def _rebuildable(value: object) -> bool:
    """Whether ``value`` can be rebuilt with replacements, so no file is saved for a
    value that will end up spilled whole."""
    try:
        _ = _substitute(value, {}, [0])
    except TypeError, ValueError:
        return False
    return True


def _json(value: object, *, indent: int | None = None) -> str:
    return to_json(value, indent=indent, fallback=repr).decode()


def _size(value: object, leaves: Sequence[str]) -> int:
    """Characters of ``value`` as the model reads it: its string leaves at their length,
    plus the serialized structure around them."""
    if isinstance(value, str):
        return len(value)
    try:
        emptied = _substitute(value, dict.fromkeys(range(len(leaves)), ""), [0])
        return sum(len(leaf) for leaf in leaves) + len(_json(emptied))
    except TypeError, ValueError:
        return len(_json(value))


def _preview(text: str, *, budget: int, untrusted: bool, note: str) -> str:
    shown = elide_middle(text, budget)
    if untrusted:
        shown = wrap_untrusted(shown)
    preview = f"{shown}\n{note}"
    assert len(preview) <= budget + _PREVIEW_OVERHEAD
    return preview


def _can_read_back(deps: RunDeps) -> bool:
    return (
        READ_BACK_TOOL not in deps.disabled_tools
        and deps.tool_access.permits(READ_BACK_TOOL)
        and READ_BACK_TOOL not in permission_disabled_tools(deps.permission)
    )


async def _write(deps: RunDeps, relpath: str, text: str) -> None:
    scratch = deps.caps.get_optional(ScratchDirs)
    if scratch is None:
        raise LookupError("no scratch folders")
    workspace = sandbox_workspace(scratch, deps.workspace_key)
    await asyncio.to_thread(workspace.files.write_file, relpath, text.encode())


async def _save(deps: RunDeps, relpath: str, text: str) -> bool:
    try:
        await asyncio.wait_for(_write(deps, relpath, text), _SAVE_TIMEOUT_S)
    except Exception:
        logger.warning("could not save an oversized tool result", exc_info=True)
        return False
    return True


async def _spilled(
    ctx: RunContext[RunDeps], text: str, *, budget: int, untrusted: bool, stem: str, save: bool
) -> str:
    """The preview standing in for ``text``, which is saved first when ``save`` and the agent
    can read the file back. Untrusted text is saved without its fence, in a file marked for it."""
    if not save:
        return _preview(text, budget=budget, untrusted=untrusted, note=_NOT_SAVED_NOTE)
    if not _can_read_back(ctx.deps):
        return _preview(text, budget=budget, untrusted=untrusted, note="[The middle was left out.]")
    suffix = UNTRUSTED_SUFFIX if untrusted else ".txt"
    relpath = f"{SPILL_DIR}/{stem}-{uuid.uuid4().hex[:8]}{suffix}"
    if await _save(ctx.deps, relpath, text):
        note = (
            f"[The full text, {len(text):,} characters, is saved at "
            + f"{SCRATCH.path}/{relpath}; read it in pieces with {READ_BACK_TOOL}.]"
        )
    else:
        note = "[The middle was left out, and the full text could not be saved.]"
    return _preview(text, budget=budget, untrusted=untrusted, note=note)


def _tool_return(result: object) -> ToolReturn[object] | None:
    return cast("ToolReturn[object]", result) if isinstance(result, ToolReturn) else None


def _script_personal_categories(result: ToolReturn[object]) -> frozenset[str] | None:
    """The personal-data categories the script's metadata shows it called, or None when the
    metadata does not show its calls, which counts as personal."""
    metadata = cast(object, result.metadata)
    if not isinstance(metadata, dict):
        return None
    calls = cast(Mapping[str, object], metadata).get("tool_calls")
    if not isinstance(calls, dict):
        return None
    found: set[str] = set()
    for call in cast(Mapping[object, object], calls).values():
        if not isinstance(call, ToolCallPart):
            return None
        if (category := _category(call.tool_name)) in PERSONAL_CATEGORIES:
            found.add(category)
    return frozenset(found)


async def _spill(ctx: RunContext[RunDeps], name: str, result: object) -> object:
    category = _category(name)
    if (
        name in _NEVER_SPILLED
        or (category in PERSONAL_CATEGORIES and category != _EXTERNAL)
        or script_call_of(ctx) is not None
    ):
        return result
    save = category not in PERSONAL_CATEGORIES
    wrapped = _tool_return(result)
    if wrapped is not None and name == RUN_CODE:
        personal = _script_personal_categories(wrapped)
        if personal is None or personal - {_EXTERNAL}:
            return result
        save = save and not personal
    value = cast(object, wrapped.return_value) if wrapped is not None else result
    if isinstance(value, Citable):
        return result
    limit = get_settings().tool_output_spill_chars
    leaves: list[str] = []
    if not _scan(value, leaves):
        return result
    size = _size(value, leaves)
    if size <= limit:
        return result
    budget = min(_PREVIEW_CHARS, limit)
    stem = _UNSAFE_NAME.sub("_", ctx.tool_call_id or "")[:_STEM_CHARS] or "call"

    swaps: dict[int, str] = {}
    rebuildable = _rebuildable(value)
    for index in sorted(range(len(leaves)), key=lambda i: len(leaves[i]), reverse=True):
        leaf = leaves[index]
        if size <= limit or len(leaf) <= budget + _PREVIEW_OVERHEAD or not rebuildable:
            break
        # Trusted text in a leaf that holds a fence is wrapped too, which fails safe.
        untrusted = _is_fenced(leaf)
        text = _MARKUP.sub("", leaf) if untrusted else leaf
        swaps[index] = await _spilled(
            ctx, text, budget=budget, untrusted=untrusted, stem=stem, save=save
        )
        size -= len(leaf) - len(swaps[index])
    if swaps:
        try:
            spilled = _substitute(value, swaps, [0])
            after: list[str] = []
            _ = _scan(spilled, after)
            previews = sum(len(preview) for preview in swaps.values())
            if isinstance(value, str) or _size(spilled, after) - previews <= limit:
                return replace(wrapped, return_value=spilled) if wrapped is not None else spilled
        except Exception:
            logger.warning("could not keep the shape of %s while spilling it", name, exc_info=True)
    if isinstance(value, str):
        return result
    text = _json(value, indent=2)
    untrusted = any(_is_fenced(leaf) for leaf in leaves) or "<untrusted" in text
    whole = await _spilled(ctx, text, budget=budget, untrusted=untrusted, stem=stem, save=save)
    return replace(wrapped, return_value=whole) if wrapped is not None else whole


async def spill_large(ctx: RunContext[RunDeps], name: str, result: object) -> object:
    """``result`` with its oversized string leaves previewed and saved, or as it was.

    The result keeps its type and shape (a ``ToolReturn`` keeps its content and metadata)
    unless what remains is still too big, when it becomes one preview of the whole value.
    A failure here never fails the tool that succeeded.
    """
    try:
        return await _spill(ctx, name, result)
    except Exception:
        logger.warning("could not spill the result of %s", name, exc_info=True)
        return result
