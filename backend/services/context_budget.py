"""Splitting the context footprint into the things that fill it.

The provider tells us one number — how many tokens the last request cost — and nothing
about what they were spent on. Everything the operator can *act* on lives in the split:
a window that is 90% messages wants a compaction or a fresh thread, one that is 60% tool
schemas wants fewer tools switched on, and the two look identical from the total alone.

So the split is measured on our side, the same way the timings are, and for the same
reason: it has to mean the same thing on Anthropic, on an OpenAI-compatible endpoint, and
on a local server. No provider reports it, so nothing here can come from one.

**Two resolutions, one measurement.** The three group totals are what the gauge's bar is
drawn from; the segments itemise those same tokens — per tool category, per contributor
to the standing brief, per class of message content — because the three-way split ends
exactly where the operator's decision begins. Nothing is measured twice: every segment
belongs to one group, and the groups are sums of their segments.

**A segment exists only if it weighs something.** There is no fixed roster of rows with
zeros in it — a thread that has never called a tool has no tool-results row, a catalog
with no connected MCP servers has no row for them, and each appears the moment it starts
costing the window. A readout of the things that are actually there is scanned; a form
with a row for everything that could be there is read.

**It is an estimate anchored to a fact.** Every piece is sized with the coarse
characters-per-token proxies the rest of the codebase already uses for soft budgets
(``core.text``, one rate for prose and a denser one for serialized structure), and then
scaled so they sum to exactly the token count the provider reported. The total is
therefore always right and the split is approximately right — which is the honest shape
of the thing, and why every figure the UI renders from this carries a `~`. Scaling also
absorbs what the proxy can't see (per-message framing, special tokens, a tokenizer that
isn't four-chars-a-token) by spreading it across the parts in proportion, rather than
letting it pile up in whichever part happens to be measured last.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from core.text import chars_to_tokens
from runs import ContextComposition, ContextSegment, TurnOverhead

from .conversation_view import message_class_chars

_Group = Literal["brief", "tools", "messages"]


@dataclass(frozen=True)
class _Piece:
    """One measured contribution, before it is converted and scaled."""

    id: str
    group: _Group
    prose: int
    structured: int
    count: int | None = None

    @property
    def estimate(self) -> float:
        """Characters as tokens, each kind at its own rate (``core.text``) — the same
        conversion the compaction trigger's footprint uses, so the gauge's bar and the
        threshold that folds the thread can never disagree about what it weighs."""
        return chars_to_tokens(self.prose, self.structured)


def compose(
    used: int | None, overhead: TurnOverhead | None, messages: list[Any]
) -> ContextComposition | None:
    """Split ``used`` — the provider's own footprint figure — into groups and segments.

    Returns None when there is nothing to split (no measured footprint) or nothing to
    split it *with* (no overhead was captured, which is the case for a turn that never
    reached a model request). Absent rather than guessed: a composition with a zeroed
    system and tools would read as "this thread carries no overhead", which is never true
    and is exactly the sort of flattering zero the readout's absent-not-zero rule exists
    to keep off the screen."""
    if not used or overhead is None:
        return None
    pieces = _pieces(overhead, messages)
    total = sum(piece.estimate for piece in pieces)
    if total <= 0:
        return None

    scaled = [round(piece.estimate / total * used) for piece in pieces]
    # Rounding shares independently can miss the total by a token or two, and a breakdown
    # that doesn't add up to the figure printed above it looks like a bug even when the
    # drift is 1. The largest piece absorbs it, being the one where a token is least
    # visible.
    scaled[scaled.index(max(scaled))] += used - sum(scaled)

    segments = tuple(
        ContextSegment(id=piece.id, group=piece.group, tokens=tokens, count=piece.count)
        for piece, tokens in zip(pieces, scaled, strict=True)
        # A piece that rounds away to nothing is a row of "0" the operator would have to
        # read past — it is present in the group total it belongs to, which is where a
        # token nobody can see should live.
        if tokens > 0
    )

    # The totals are the sums of the rows beneath them rather than a second reckoning of
    # the same tokens — the bar and the list are one measurement, and two derivations of
    # it would eventually disagree by a token and make the operator arbitrate.
    def total_for(group: _Group) -> int:
        return sum(segment.tokens for segment in segments if segment.group == group)

    return ContextComposition(
        system=total_for("brief"),
        tools=total_for("tools"),
        messages=total_for("messages"),
        segments=segments,
    )


def _pieces(overhead: TurnOverhead, messages: list[Any]) -> list[_Piece]:
    """Everything in the window, itemised as far as it was measured.

    Falls back to a group's total whenever its itemisation is missing — an overhead
    stored before this measurement existed, or one whose detail a library change put out
    of reach. The coarse reading survives on its own that way; only the rows beneath it
    are lost, which is the right thing to lose."""
    pieces: list[_Piece] = []

    if overhead.blocks:
        pieces.extend(
            _Piece(id=block.id, group="brief", prose=block.chars, structured=0)
            for block in overhead.blocks
        )
    elif overhead.system:
        pieces.append(_Piece(id="base", group="brief", prose=overhead.system, structured=0))

    if overhead.groups:
        pieces.extend(
            _Piece(
                id=group.category,
                group="tools",
                prose=0,
                structured=group.chars,
                count=group.tools,
            )
            for group in overhead.groups
        )
    elif overhead.tools:
        pieces.append(_Piece(id="tools", group="tools", prose=0, structured=overhead.tools))

    pieces.extend(
        _Piece(id=name, group="messages", prose=chars.prose, structured=chars.structured)
        for name, chars in message_class_chars(messages).items()
    )
    return pieces
