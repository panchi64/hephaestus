"""The compaction summary's section contract, as a parser.

``prompts/utility.py`` asks the summarizer for a fixed roster of headings, and that roster is a
**parsed contract rather than a style guide**: the carry-forward reads `Anchors` out of it
verbatim, and `fence_tool_facts` uses it to decide where the untrusted fence closes. Both
of those, and the operator's own transcript, need the same answer to "what are this
checkpoint's sections" — so the parse lives here, once, below everything that wants it.

**Why this is a separate module from the code that produces summaries.**
``agent/compaction_summary.py`` imports ``services.conversation_view``, so anything in
``services`` (the conversation projection, which renders a checkpoint on a cold read) could
not import the parser back from ``agent`` without a cycle. This module imports only
``prompts.compaction`` and ``core.untrusted``, both leaves, so the producer, the live event and
the cold-read projection can all reach it.

**The roster is a security boundary, not a convenience.** The summarizer is asked to quote
its sources, so a fetched page's own ``## Notes for the assistant`` arrives inside the
summary looking exactly like a heading. Only a heading naming one of *our* sections ends a
section; anything else is body text wherever it appears. Getting that wrong closes the
untrusted fence early and leaves whatever followed it stored as the workspace's own voice,
which is the injection the fence exists to stop.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel

from prompts.compaction import (
    COMPACT_ANCHORS_SECTION,
    COMPACT_CLOSE,
    COMPACT_MARKER,
    COMPACT_PREAMBLE,
    COMPACT_SECTIONS,
    COMPACT_TOOLS_SECTION,
    LEGACY_COMPACT_MARKER,
    LEGACY_COMPACT_PREAMBLE,
)

# Sections are asked for as `## Name` lines; anything else in the text is body. A model
# that reaches for bold instead of hashes, or repeats the heading's gloss after the name,
# is still writing the section we asked for — so the pattern accepts both and the name is
# matched by prefix. Nothing here fails loudly: a summary whose headings don't parse simply
# gets no carry-forward and no fence, which is what the previous format got.
_HEADING = re.compile(
    r"^[ \t]*(?:#{1,3}|\*\*)[ \t]*(?P<name>[^\n#*]+?)[ \t]*\**[ \t]*:?[ \t]*$", re.MULTILINE
)

# …but only a heading naming one of *our* sections ends a section. See the module docstring:
# anything not on the roster is body text, wherever it appears.
_SECTION_KEYS = tuple(re.sub(r"[^a-z0-9]+", " ", name.lower()).strip() for name in COMPACT_SECTIONS)

# A fence marker, either end. Used to strip a previous checkpoint's quoted tool content
# before its Anchors section is read: the summary that carried it may have omitted its own
# Anchors heading (the prompt allows omission), and then the first `## Anchors` in the text
# is one the fenced page wrote — lifting *those* lines forward would launder an injection
# into every later checkpoint, verbatim, forever.
#
# Matches the square-bracket shape too: checkpoints stored before the tag switch carry it.
_FENCE_MARKER = re.compile(
    r"\[(?P<legacy>BEGIN|END) UNTRUSTED CONTENT\b[^\]]*\]|<(?P<close>/)?untrusted\b[^>]*>"
)


def _opens(marker: re.Match[str]) -> bool:
    """Whether a ``_FENCE_MARKER`` match opens a fence rather than closing one."""
    legacy = marker.group("legacy")
    return legacy == "BEGIN" if legacy else marker.group("close") is None


def section_key(name: str) -> str:
    """A heading (or any line) reduced to the form the roster is matched on — lowercase,
    punctuation and runs of space collapsed. Public because the anchors carry-forward
    dedupes on it: the same anchor rewritten as ``- x`` and ``* x`` is one anchor."""
    return re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()


def _known_key(name: str) -> str | None:
    """The section this heading names, or ``None`` when it names none of ours."""
    key = section_key(name)
    return next((section for section in _SECTION_KEYS if key.startswith(section)), None)


def _span(text: str, name: str) -> tuple[int, int] | None:
    """Where the named section's body starts and ends, matching the heading loosely (case,
    punctuation and any restated gloss are the model's choice, the section is ours).

    The section runs to the next heading **we asked for**, or to the end of the text — see
    ``_SECTION_KEYS``: a heading-shaped line the summarizer copied out of a tool result is
    part of that result, not the start of a new section."""
    wanted = section_key(name)
    matches = [m for m in _HEADING.finditer(text) if _known_key(m.group("name")) is not None]
    for index, match in enumerate(matches):
        if not section_key(match.group("name")).startswith(wanted):
            continue
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        return start, end
    return None


def section_body(text: str, name: str) -> str | None:
    """The body under the ``## name`` heading, or ``None`` when there is no such heading."""
    span = _span(text, name)
    if span is None:
        return None
    start, end = span
    return text[start:end].strip()


def replace_section(text: str, name: str, body: str) -> str:
    """``text`` with the named section's body replaced. Returns it unchanged when the
    heading isn't there — the caller's job is to notice, not this one's."""
    span = _span(text, name)
    if span is None:
        return text
    start, end = span
    return "\n".join([text[:start].rstrip("\n"), body, text[end:].lstrip("\n")]).rstrip()


def without_fenced(text: str) -> str:
    """``text`` with everything between (and including) the untrusted-content markers
    dropped, an unclosed fence taking the rest of the text with it.

    A checkpoint is replayed as a user-shaped message — the most authoritative voice in the
    history — and its fenced section is the one part that repeats what a web page said. The
    carry-forward copies lines **verbatim** into the next checkpoint, outside any fence, so
    it must never be able to read a line out of one. Erring long is deliberate: dropping a
    genuine anchor costs a paraphrase, promoting a fetched instruction costs the fence."""
    out: list[str] = []
    fenced = False
    for line in text.splitlines():
        marker = _FENCE_MARKER.search(line)
        if marker is not None:
            fenced = _opens(marker)
            continue
        if not fenced:
            out.append(line)
    return "\n".join(out)


def _fenced_content(body: str) -> str:
    """What a fenced section actually quoted — the lines *between* the markers, with the
    ``untrusted_preamble`` that introduces them dropped.

    The inverse of what ``fence_tool_facts`` wrote, and the operator-facing counterpart to
    :func:`without_fenced`: that one keeps the workspace's own voice and throws the quote
    away, this one keeps the quote. The UI needs it because the fence's *markers* are
    addressed to the model — a nonce the operator has no use for — while the text inside
    them is the only record of what a tool returned. Marking it as tool-derived is the
    renderer's job (``untrusted``), so the nonce carries no information here.

    An unclosed fence takes the rest of the body, matching :func:`without_fenced`'s
    err-long rule from the other side."""
    lines = body.splitlines()
    begin = next(
        (i for i, line in enumerate(lines) if (m := _FENCE_MARKER.search(line)) and _opens(m)),
        None,
    )
    if begin is None:
        # No fence: an old checkpoint, or a section the summarizer left empty. Drop any
        # stray marker rather than showing one.
        return _FENCE_MARKER.sub("", body).strip()
    end = next(
        (
            i
            for i, line in enumerate(lines[begin + 1 :], start=begin + 1)
            if (m := _FENCE_MARKER.search(line)) and not _opens(m)
        ),
        len(lines),
    )
    return "\n".join(lines[begin + 1 : end]).strip()


class SummarySection(BaseModel):
    """One section of a stored checkpoint, ready to render.

    ``key`` is the roster heading verbatim, or **empty** for the one synthetic section a
    checkpoint that parses into nothing degrades to (see :func:`summary_sections`) — a
    renderer shows that one without a heading.

    ``untrusted`` is set by **section identity**, never by finding a fence: a checkpoint
    written before ``fence_tool_facts`` existed still repeats what a web page said, and
    must still be attributed. ``voice`` is how the section wants to be set —
    ``"machine"`` for Anchors, whose whole purpose is exact paths, ids and numbers."""

    key: str
    body: str
    untrusted: bool = False
    voice: Literal["prose", "machine"] = "prose"


def is_checkpoint(text: str) -> bool:
    """Whether ``text`` is a stored compaction summary, in the tagged or the legacy form."""
    return text.lstrip().startswith((COMPACT_MARKER, LEGACY_COMPACT_MARKER))


def strip_preamble(text: str) -> str:
    """``text`` without the label the store wrapped it in.

    The label is addressed to the model; the operator's transcript already draws the
    divider. Legacy checkpoints carry the bracketed marker, some without the note line."""
    body = text.strip().removesuffix(COMPACT_CLOSE).rstrip()
    prefixes = (COMPACT_PREAMBLE, LEGACY_COMPACT_PREAMBLE, COMPACT_MARKER, LEGACY_COMPACT_MARKER)
    for prefix in prefixes:
        if body.startswith(prefix):
            return body[len(prefix) :].strip()
    return body


def summary_sections(text: str) -> list[SummarySection]:
    """Split a stored checkpoint into the sections a renderer should show.

    Only sections actually present are returned, in roster order — the prompt allows the
    summarizer to omit one the conversation said nothing about, and an omitted section is not
    an empty one. The ``COMPACT_PREAMBLE`` is never a section; neither are the fence
    markers, whose nonce is addressed to the model.

    **Degrades to one keyless section** holding the whole preamble-stripped text when no
    roster heading matches — an old checkpoint, or a summarizer that drifted off the
    format. That keeps one rendering path rather than making every caller carry a fallback,
    and it never claims a structure the text does not have. Empty in and empty out."""
    body = strip_preamble(text)
    if not body:
        return []
    sections: list[SummarySection] = []
    for name in COMPACT_SECTIONS:
        raw = section_body(body, name)
        if raw is None:
            continue
        untrusted = name == COMPACT_TOOLS_SECTION
        content = _fenced_content(raw) if untrusted else _FENCE_MARKER.sub("", raw).strip()
        if not content:
            continue
        sections.append(
            SummarySection(
                key=name,
                body=content,
                untrusted=untrusted,
                voice="machine" if name == COMPACT_ANCHORS_SECTION else "prose",
            )
        )
    if not sections:
        return [SummarySection(key="", body=body)]
    return sections
