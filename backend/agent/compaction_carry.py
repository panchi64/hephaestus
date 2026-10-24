"""What a fold copies into the checkpoint verbatim, in code, rather than asking the model to
restate it.

Each pass of a model over a summary paraphrases a little more, so the two things a thread
cannot afford to drift are carried mechanically:

- **Anchors**: exact paths, ids, names and numbers. A second fold lifts the previous
  checkpoint's Anchors lines forward unchanged.
- **Operator messages**: what the operator actually typed, newest first up to a token
  budget, copied from the folded requests and from the previous checkpoint.

Both read a previous checkpoint only **with its fenced regions removed** (``without_fenced``):
these sections are replayed outside any fence, so a line lifted out of one would turn a
fetched page's text into the workspace's own voice.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from pydantic_ai import (
    ModelMessage,
    ModelRequest,
    ToolReturnPart,
    UserPromptPart,
)

from core.compaction_sections import (
    escape_headings,
    insert_section,
    is_checkpoint,
    section_body,
    section_key,
    strip_preamble,
    unescape_headings,
    without_fenced,
)
from core.text import CHARS_PER_TOKEN_PROSE, chars_to_tokens
from core.untrusted import neutralize_tags
from prompts.compaction import (
    COMPACT_ANCHORS_SECTION,
    COMPACT_OPERATOR_SECTION,
    COMPACT_OPERATOR_TAG,
)
from services.conversation_view import flatten_content, strip_chassis_markers
from services.notices import ENVELOPE_TAGS, notice_body

_MESSAGE_TAG = COMPACT_OPERATOR_TAG
_MESSAGE = re.compile(
    rf'<{_MESSAGE_TAG} at="(?P<at>[^"]*)">\n(?P<body>.*?)\n</{_MESSAGE_TAG}>', re.DOTALL
)
_DROPPED_NOTE = "Earlier operator messages are summarized under Operator requests."
# Chassis-written user parts open with one of these; none of them is the operator typing.
_CHASSIS_OPENERS = ("<workspace-note>", *(f"<{tag}" for tag in ENVELOPE_TAGS))


@dataclass(frozen=True)
class OperatorMessage:
    """One message the operator typed, as copied into a checkpoint."""

    at: str
    text: str


def carried_anchors(messages: list[ModelMessage]) -> list[str]:
    """The Anchors lines of any checkpoint among the messages being folded."""
    lines: list[str] = []
    for text in _checkpoint_texts(messages):
        lines.extend(_bullets(section_body(text, COMPACT_ANCHORS_SECTION)))
    return _dedupe(lines)


def merge_anchors(summary: str, carried: list[str]) -> str:
    """Fold ``carried`` anchor lines into the summary's Anchors section, keeping the new
    ones first and dropping duplicates. Appends the section when the summary has none."""
    if not carried:
        return summary
    existing = _bullets(section_body(summary, COMPACT_ANCHORS_SECTION))
    merged = _dedupe(existing + carried)
    if merged == existing:
        return summary
    return insert_section(summary, COMPACT_ANCHORS_SECTION, "\n".join(merged))


def operator_messages(messages: list[ModelMessage]) -> tuple[list[OperatorMessage], bool]:
    """Every operator message the fold covers, oldest first, and whether an earlier
    checkpoint already dropped some for the budget.

    A previous checkpoint's copies come first, then the folded requests' own. A request
    carrying a tool return is the agent's round-trip (a screenshot's pixels ride as user
    content), and chassis parts (checkpoints, notices, envelopes, workspace notes) are not
    the operator's words."""
    out: list[OperatorMessage] = []
    dropped = False
    for message in messages:
        if not isinstance(message, ModelRequest):
            continue
        if any(isinstance(part, ToolReturnPart) for part in message.parts):
            continue
        for part in message.parts:
            if not isinstance(part, UserPromptPart):
                continue
            text = flatten_content(part.content).strip()
            if is_checkpoint(text):
                body = section_body(without_fenced(strip_preamble(text)), COMPACT_OPERATOR_SECTION)
                if body:
                    out.extend(_parse_messages(body))
                    dropped = dropped or _DROPPED_NOTE in body
                continue
            if notice_body(text) is not None or text.startswith(_CHASSIS_OPENERS):
                continue
            typed = without_fenced(strip_chassis_markers(text)).strip()
            if typed:
                out.append(OperatorMessage(at=_stamp(part.timestamp), text=typed))
    return out, dropped


def merge_operator_messages(
    summary: str, messages: list[ModelMessage], *, budget_tokens: int
) -> str:
    """The summary with its Operator messages section filled from ``messages``, newest
    first within ``budget_tokens``.

    Replaces a section the model wrote despite being told not to, and otherwise inserts it
    at its roster position. A newest message that alone exceeds the budget keeps its head
    and tail around a cut marker."""
    found, dropped = operator_messages(messages)
    if not found:
        return summary
    kept = _within_budget(found, budget_tokens)
    dropped = dropped or len(kept) < len(found)
    blocks = [_render(message) for message in kept]
    if dropped:
        blocks.insert(0, _DROPPED_NOTE)
    return insert_section(summary, COMPACT_OPERATOR_SECTION, "\n\n".join(blocks))


def _within_budget(found: list[OperatorMessage], budget_tokens: int) -> list[OperatorMessage]:
    """The newest messages that fit, oldest first; the newest is cut rather than dropped."""
    kept: list[OperatorMessage] = []
    spent = 0.0
    for message in reversed(found):
        cost = chars_to_tokens(len(message.text))
        if spent + cost <= budget_tokens:
            kept.append(message)
            spent += cost
            continue
        if not kept:
            kept.append(OperatorMessage(at=message.at, text=_cut(message.text, budget_tokens)))
        break
    kept.reverse()
    return kept


def _cut(text: str, budget_tokens: int) -> str:
    """``text`` cut to its head and tail around a marker, within ``budget_tokens``."""
    room = max(0, int(budget_tokens * CHARS_PER_TOKEN_PROSE) - 40)
    head, tail = room // 2, room - room // 2
    removed = len(text) - head - tail
    return f"{text[:head]}\n[… {removed} characters cut]\n{text[len(text) - tail :]}"


def _render(message: OperatorMessage) -> str:
    # Escaped so an operator who typed `## Anchors` does not end this section.
    escaped = escape_headings(neutralize_tags(message.text, (_MESSAGE_TAG,)))
    return f'<{_MESSAGE_TAG} at="{message.at}">\n{escaped}\n</{_MESSAGE_TAG}>'


def _parse_messages(body: str) -> list[OperatorMessage]:
    return [
        OperatorMessage(at=match["at"], text=unescape_headings(match["body"]))
        for match in _MESSAGE.finditer(body)
    ]


def _stamp(timestamp: datetime) -> str:
    return timestamp.isoformat(timespec="minutes")


def _bullets(body: str | None) -> list[str]:
    """A section's non-empty lines, as written."""
    if not body:
        return []
    return [line.rstrip() for line in body.splitlines() if line.strip()]


def _dedupe(lines: list[str]) -> list[str]:
    """Order-preserving dedupe on a line's words, so "- x" and "* x" are one anchor."""
    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        key = section_key(line)
        if key and key not in seen:
            seen.add(key)
            out.append(line)
    return out


def _checkpoint_texts(messages: list[ModelMessage]) -> list[str]:
    """The stored checkpoint texts among a fold's messages, newest last, with every fenced
    region removed."""
    texts: list[str] = []
    for message in messages:
        if not isinstance(message, ModelRequest):
            continue
        for part in message.parts:
            if isinstance(part, UserPromptPart):
                text = flatten_content(part.content).strip()
                if is_checkpoint(text):
                    texts.append(without_fenced(strip_preamble(text)))
    return texts
