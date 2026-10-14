"""Splitting a markdown file's YAML frontmatter from the body underneath it.

Two file formats this codebase reads open with the same ``---`` block and mean entirely
different things below it: a skill's ``SKILL.md``, and an agent definition a project
declares for its own sub-agents. Neither cares *how* the fence is found, and both were
going to get the same twenty lines — a BOM strip, a fence match that must be a whole line,
a ``yaml.safe_load`` that has to be told a stream of ``None`` is an empty mapping, and a
body whose leading newlines are an artifact of the fence rather than content.

So the finding lives here, once, and the *meaning* lives with each format. What a field is
called, whether it is required and what it may hold are the caller's business; this module
answers only "is there a frontmatter block, and what is in it".

Deliberately not a validator. It raises on a file that is not of this shape at all, and
says nothing about a file that is — a caller that wanted a missing key to be fatal and one
that wanted it defaulted are both right, and neither is decidable from here.
"""

from __future__ import annotations

import re
from typing import cast

import yaml

#: The fence, both opening and closing.
FENCE = "---"

#: The closing fence has to be a line of its own. Anchored and multiline so a ``---``
#: inside a YAML value — a horizontal rule in a folded block scalar, most likely — cannot
#: end the block early.
_CLOSING = re.compile(r"^---[ \t]*$", re.MULTILINE)


class FrontmatterError(ValueError):
    """The text does not carry a readable ``---`` YAML block."""


def split_frontmatter(text: str) -> tuple[dict[str, object], str]:
    """The frontmatter fields and the body below them.

    The leading BOM strip is not defensive decoration: these files are written by hand, on
    every platform, and one saved from a Windows editor opens with ``\\ufeff`` before the
    fence — which would otherwise read as "no frontmatter at all" and reject a file whose
    only fault is where it was typed.
    """
    stripped = text.lstrip("﻿").lstrip()
    if not stripped.startswith(FENCE):
        raise FrontmatterError(f"must open with a {FENCE!r} YAML frontmatter block")
    rest = stripped[len(FENCE) :].lstrip("\r\n")
    closing = _CLOSING.search(rest)
    if closing is None:
        raise FrontmatterError(f"the {FENCE!r} frontmatter block is never closed")
    body = rest[closing.end() :].lstrip("\r\n").rstrip()
    try:
        loaded: object = cast("object", yaml.safe_load(rest[: closing.start()])) or {}
    except yaml.YAMLError as exc:
        raise FrontmatterError(f"frontmatter is not valid YAML: {exc}") from None
    if not isinstance(loaded, dict):
        raise FrontmatterError("frontmatter must be a mapping of fields")
    return cast("dict[str, object]", loaded), body
