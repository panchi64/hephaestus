"""Splitting the envelopes the backend wraps text in into typed parts a client can collapse.

Untrusted fences and context-file blocks reach the model as tagged text; the operator
should see them as labelled rows, not tag noise. One splitter serves both the reload
projection and the live ``tool.completed`` frame, so the two draw the same rows.
Shape-matched, like :func:`core.untrusted.unfence`: a presentation split, never a
security decision.
"""

from __future__ import annotations

import json
import re
from typing import Literal

from pydantic import BaseModel, TypeAdapter, ValidationError

from core.untrusted import PREAMBLE_LINE_CAPTURED

#: The key a dict tool result carries a project's instruction brief under.
BRIEF_KEY = "project_instructions"

_PAYLOAD = TypeAdapter(dict[str, object])

_ENVELOPE = re.compile(
    rf"""
    (?:^{PREAMBLE_LINE_CAPTURED}\n(?=<untrusted\ id=))?
    (?:
        <untrusted\ id="(?P<nonce>[^"]*)"(?:\ source="(?P<source>[^"]*)")?>\n
        (?P<body>.*?)\n
        </untrusted\ id="(?P=nonce)">
      |
        (?:^How\ to\ work\ in\ ~[^\n]*,\ from\ its\ own\ instruction\ files:\n\n)?
        <context-file\ path="(?P<path>[^"]*)">\n
        (?P<file>.*?)\n
        </context-file>
    )
    """,
    re.DOTALL | re.MULTILINE | re.VERBOSE,
)


class ContentPart(BaseModel):
    kind: Literal["text", "untrusted", "context_file"]
    text: str
    source: str | None = None
    path: str | None = None


def _text(text: str) -> list[ContentPart]:
    stripped = text.strip()
    return [ContentPart(kind="text", text=stripped)] if stripped else []


def split_envelopes(text: str, *, vouched_only: bool = False) -> list[ContentPart]:
    """``text`` as ordered parts: untrusted fences and context-file blocks lifted out
    with their wrapper lines dropped, everything between them as text.

    ``vouched_only`` is for text the operator typed into: only a fence led by its own
    preamble (what :func:`core.untrusted.wrap_untrusted` writes, nonce for nonce) is lifted,
    so a block the operator pasted stays in their words where they put it."""
    parts: list[ContentPart] = []
    cursor = 0
    for match in _ENVELOPE.finditer(text):
        if vouched_only and (
            match["nonce"] is None or match["preamble_nonce"] != match["nonce"]
        ):
            continue
        parts.extend(_text(text[cursor : match.start()]))
        cursor = match.end()
        if match["nonce"] is not None:
            source = match["source"]
            parts.append(
                ContentPart(
                    kind="untrusted",
                    text=match["body"],
                    source=None if source is None else source.replace("&quot;", '"'),
                )
            )
        else:
            parts.append(ContentPart(kind="context_file", text=match["file"], path=match["path"]))
    parts.extend(_text(text[cursor:]))
    return parts


def result_parts(result: object) -> list[ContentPart] | None:
    """The parts of a tool ``result`` that carries an envelope, or None when it carries none."""
    if isinstance(result, str):
        parts = split_envelopes(result)
        return parts if any(part.kind != "text" for part in parts) else None
    if isinstance(result, dict):
        try:
            payload = _PAYLOAD.validate_python(result)
        except ValidationError:
            return None
        brief = payload.get(BRIEF_KEY)
        if not isinstance(brief, str):
            return None
        rest = {key: value for key, value in payload.items() if key != BRIEF_KEY}
        head = _text(json.dumps(rest, indent=2, default=str, ensure_ascii=False)) if rest else []
        return [*head, *split_envelopes(brief)]
    return None
