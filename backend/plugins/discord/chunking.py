"""Splitting an answer into messages Discord accepts, without breaking a code fence."""

from __future__ import annotations

DISCORD_LIMIT = 2000
_FENCE = "```"
_CLOSER = "\n" + _FENCE
_MAX_LANG = 20


def chunk(text: str, limit: int = DISCORD_LIMIT) -> list[str]:
    """Pieces of at most ``limit`` characters, cut at paragraph breaks, then line breaks,
    then anywhere. A fence a cut lands inside is closed there and reopened, with its
    language tag, at the start of the next piece."""
    if limit < len(_FENCE) + _MAX_LANG + len(_CLOSER) + 2:
        raise ValueError("limit is too small to hold a fenced piece")
    rest = text.strip()
    pieces: list[str] = []
    lang: str | None = None
    while rest:
        prefix = "" if lang is None else f"{_FENCE}{lang}\n"
        if len(prefix) + len(rest) <= limit:
            pieces.append(prefix + rest)
            break
        cut = _cut(rest, limit - len(prefix) - len(_CLOSER))
        body = rest[:cut].rstrip()
        rest = rest[cut:].lstrip("\n")
        lang = _fence_after(lang, body)
        if body:
            pieces.append(prefix + body + (_CLOSER if lang is not None else ""))
    return pieces


def _cut(text: str, room: int) -> int:
    window = text[:room]
    for separator in ("\n\n", "\n"):
        at = window.rfind(separator)
        if at > 0:
            return at
    return room


def _fence_after(lang: str | None, body: str) -> str | None:
    """The open fence's language tag after ``body`` (``""`` when untagged), or ``None``."""
    for line in body.split("\n"):
        stripped = line.strip()
        if stripped.startswith(_FENCE):
            lang = None if lang is not None else stripped[len(_FENCE) :].strip()[:_MAX_LANG]
    return lang
