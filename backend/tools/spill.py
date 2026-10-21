"""Oversized tool results go to the thread's scratch; the model gets a preview and the path.

A result past ``tool_output_spill_chars`` would flood the context if returned whole, and
cutting it silently loses what the model may need. The full text is saved under
``tool-output/`` in the scratch, which every thread mounts at ``~/scratch`` whether or
not it is filed under a project, so ``files_read_file`` pages through it on any thread.
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import cast

from pydantic_ai import RunContext, ToolReturn
from pydantic_ai.messages import is_multi_modal_content
from pydantic_core import to_json

from core.config import get_settings
from core.text import truncate_middle
from core.untrusted import PREAMBLE_LINE, unwrap_untrusted, wrap_untrusted
from services.home.paths import SCRATCH
from services.sandbox import SandboxError, SandboxSessionManager
from services.workspace import sandbox_workspace

from .deps import RunDeps

logger = logging.getLogger(__name__)

SPILL_DIR = "tool-output"

#: The library's name for the tool, and the one name everything keyed on it uses.
RUN_CODE = "run_code"

#: Results the model reads in pieces already, or that hold personal data scratch must not.
_NEVER_SPILLED = frozenset({"files_read_file"})
_NEVER_SPILLED_PREFIXES = ("mail_",)

_PREVIEW_CHARS = 8_000
_FENCE_OPEN = re.compile(r'<untrusted id="[0-9a-f]+"')
_PREAMBLE = re.compile(PREAMBLE_LINE)
_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]")


def _holds_media(value: object) -> bool:
    if is_multi_modal_content(value) or isinstance(value, bytes | bytearray):
        return True
    if isinstance(value, list | tuple):
        return any(_holds_media(item) for item in cast(Sequence[object], value))
    if isinstance(value, dict):
        return any(_holds_media(item) for item in cast(Mapping[object, object], value).values())
    return False


def _text_of(value: object) -> str | None:
    """The result as text, or None when it carries media the model reads natively."""
    if isinstance(value, str):
        return value
    if _holds_media(value):
        return None
    try:
        return to_json(value, fallback=repr).decode()
    except ValueError:
        return None


def _tool_return(result: object) -> ToolReturn[object] | None:
    return cast("ToolReturn[object]", result) if isinstance(result, ToolReturn) else None


def _is_untrusted(text: str) -> bool:
    return _FENCE_OPEN.search(text) is not None or _PREAMBLE.search(text) is not None


def script_call_of(ctx: RunContext[RunDeps]) -> str | None:
    """The ``run_code`` call a tool hook's call was made from, or None for a direct call.

    The sandbox dispatches through a ``ToolManager`` of its own whose context *is* the
    ``run_code`` call's, and the library hands each call a context carrying the manager
    executing it. So a call is nested exactly when its manager's context names
    ``run_code``; a direct call's manager is the run's, whose context names no tool.
    """
    manager = ctx.tool_manager
    outer = manager.ctx if manager is not None else None
    if outer is not None and outer.tool_name == RUN_CODE:
        return outer.tool_call_id
    return None


def _skipped(name: str, ctx: RunContext[RunDeps]) -> bool:
    if name in _NEVER_SPILLED or name.startswith(_NEVER_SPILLED_PREFIXES):
        return True
    return script_call_of(ctx) is not None


async def _save(deps: RunDeps, relpath: str, text: str) -> bool:
    sessions = deps.caps.get_optional(SandboxSessionManager)
    if sessions is None:
        return False
    try:
        workspace = await sandbox_workspace(sessions, deps.workspace_key, holder=deps.run)
        await asyncio.to_thread(workspace.files.write_file, relpath, text.encode())
    except (SandboxError, OSError):
        logger.warning("could not save an oversized tool result", exc_info=True)
        return False
    return True


def _preview(text: str, *, path: str, limit: int) -> str:
    untrusted = _is_untrusted(text)
    inner = unwrap_untrusted(text) if untrusted else text
    head, tail, elided = truncate_middle(inner, min(_PREVIEW_CHARS, limit))
    body = f"{head}\n[… {elided:,} characters omitted …]\n{tail}"
    if untrusted:
        body = wrap_untrusted(body)
    note = (
        f"[The result was {len(text):,} characters. The full text is saved at {path}; "
        + "read it in pieces with files_read_file.]"
    )
    return f"{body}\n{note}"


async def spill_large(ctx: RunContext[RunDeps], name: str, result: object) -> object:
    """``result`` unchanged, or its preview when it is too long and has been saved.

    A ``ToolReturn`` keeps its ``content`` and ``metadata``; only its value is replaced.
    A result that cannot be saved, or that is skipped, goes back whole.
    """
    limit = get_settings().tool_output_spill_chars
    if _skipped(name, ctx):
        return result
    wrapped = _tool_return(result)
    value = cast(object, wrapped.return_value) if wrapped is not None else result
    text = _text_of(value)
    if text is None or len(text) <= limit:
        return result
    stem = _UNSAFE_NAME.sub("_", ctx.tool_call_id or "") or uuid.uuid4().hex
    relpath = f"{SPILL_DIR}/{stem}.txt"
    if not await _save(ctx.deps, relpath, text):
        return result
    preview = _preview(text, path=f"{SCRATCH.path}/{relpath}", limit=limit)
    if wrapped is not None:
        return replace(wrapped, return_value=preview)
    return preview
