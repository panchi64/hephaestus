"""Tiny text/token utilities shared across layers.

There is no exact tokenizer at the points these are used (conversation compaction's
footprint estimate, the context readout's split, the summarizer's input budget), and the
budgets they serve are *soft*, so a coarse characters-per-token proxy is good enough to
bound sizes deterministically. Kept here, in the foundation layer, so every caller shares
one estimate, not several — the gauge and the trigger have to agree about how full a
thread is, and they only do if they divide by the same numbers.

:func:`replace_unique` is here for the same reason: surgical editing is the shape of both
``DOC-2`` and ``SKILL-3``, and one implementation of "replace exactly one span or refuse"
means both surfaces refuse identically. So is :func:`strip_think_blocks` — every utility
model call that asks for reasoning off has to survive a runtime that ignores the lever.
"""

from __future__ import annotations

import re

from .exceptions import SpanEditError

# A coarse characters≈tokens proxy, for turning a *token* budget back into a character
# one (`tokens_to_chars`). Deliberately the pessimistic member of the pair below: a
# budget converted at 4 asks for fewer characters than the content will actually fit, and
# a soft budget that under-fills is a budget nothing overflows.
CHARS_PER_TOKEN = 4

#: Characters per token, by content kind — measured against cl100k on representative
#: samples and pinned by `tests/test_context_estimator_calibration.py`.
#:
#: Two rates rather than one because the difference between them is load-bearing wherever
#: prose and serialized structure are *compared*: JSON spends about a third of its
#: characters on punctuation and short repeated keys, so it tokenizes denser than prose,
#: and a shared divisor leaves the prose parts inflated by roughly a fifth against the
#: JSON ones. On a tool-heavy thread that is the difference between "your tools are the
#: problem" and "your conversation is" — and, since the same numbers drive the compaction
#: trigger, between folding a thread and letting it overflow.
#:
#: They live here, beside `CHARS_PER_TOKEN`, because every measurement in the codebase
#: has to answer with the same number: the context readout's split, the footprint the
#: compaction trigger projects, and the gauge the operator reads are one estimate seen
#: three ways, and three private copies of these constants would eventually disagree.
#:
#: Code sits between the two (~4.3) and is counted as prose, since it arrives as a string.
#: That over-credits a thread of pasted source by around a tenth, which is inside the
#: tolerance a `~` readout claims and well short of the error a shared divisor produces.
CHARS_PER_TOKEN_PROSE = 4.8
CHARS_PER_TOKEN_JSON = 4.1


def chars_to_tokens(prose: int, structured: int = 0) -> float:
    """Characters as tokens, each kind converted at its own rate.

    Unrounded on purpose: callers sum many of these before they round once, and rounding
    each contribution would accumulate a bias in whichever direction the parts lean."""
    return prose / CHARS_PER_TOKEN_PROSE + structured / CHARS_PER_TOKEN_JSON


# A reasoning model that the runtime didn't keep off inlines its chain-of-thought as a
# ``<think>…</think>`` block in the *content* (rather than a separate reasoning channel
# Pydantic AI would surface as a ``ThinkingPart``). The close is optional (``$`` under
# DOTALL): a model that exhausts ``max_tokens`` while still reasoning emits an *unclosed*
# ``<think>`` whose partial content Pydantic AI still returns — strip that to end-of-string
# so a half-thought can never be mistaken for the answer. Case-insensitive, since the tag
# casing is the model/template's choice, not ours.
_THINK_BLOCK = re.compile(r"<think>.*?(?:</think>|$)", re.DOTALL | re.IGNORECASE)


def strip_think_blocks(text: str) -> str:
    """Drop any inlined ``<think>…</think>`` reasoning from a model's text output.

    The reasoning-off lever is **best-effort, not guaranteed** — LM Studio drops OpenAI
    ``chat_template_kwargs``, the Qwen 2507+ line dropped the ``/no_think`` soft-switch —
    so every utility call that asks for reasoning off (the conversation namer, the
    compaction summarizer) has to be able to read past a think block it didn't want. One
    implementation, so a runtime that leaks reasoning leaks it into no surface at all.
    No-op when there's no think block."""
    return _THINK_BLOCK.sub("", text)


def replace_unique(
    text: str, old: str, new: str, *, error: type[SpanEditError] = SpanEditError
) -> str:
    """Replace the **single** occurrence of ``old`` in ``text`` with ``new``.

    Raises ``error`` (a :class:`SpanEditError` subclass, carrying the occurrence count) when
    ``old`` is absent or matches more than once, so the caller can ask for a more precise
    span rather than guessing which match was meant. Callers run this inside their write
    transaction so the check and the replace are atomic."""
    occurrences = text.count(old)
    if occurrences != 1:
        raise error(occurrences)
    return text.replace(old, new, 1)


def tokens_to_chars(tokens: int) -> int:
    """The character budget for a token budget, floored at zero."""
    return max(0, tokens) * CHARS_PER_TOKEN


def truncate_on_boundary(text: str, max_chars: int) -> str:
    """The first ``max_chars`` of ``text``, trimmed back to a nearby whitespace boundary when
    one is reasonably close, so the cut doesn't split a word mid-token."""
    if max_chars <= 0 or len(text) <= max_chars:
        return text[:max_chars]
    cut = text[:max_chars]
    boundary = max(cut.rfind(" "), cut.rfind("\n"))
    if boundary > max_chars * 0.8:
        cut = cut[:boundary]
    return cut.rstrip()


def clip(value: str, limit: int) -> str:
    """``value`` at no more than ``limit`` characters, ending in an ellipsis if it was cut.

    The ellipsis is the point: a silently cut line reads as the whole of what was said.
    """
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


def truncate_middle(text: str, max_chars: int) -> tuple[str, str, int]:
    """Cap ``text`` to ``max_chars`` by keeping a **head and a tail** and eliding the
    middle — the mirror image of :func:`truncate_on_boundary`, which keeps only the
    head. Useful when the interesting part could be either the setup (head) or the
    final state/error (tail, where a failing process's output usually lands).

    Returns ``(head, tail, elided_chars)``. When ``text`` already fits, ``head`` is
    the text unchanged, ``tail`` is empty, and ``elided_chars`` is 0 — the caller can
    always safely use ``head`` alone in that case, and insert its own marker between
    ``head``/``tail`` only when ``elided_chars`` is nonzero."""
    if max_chars <= 0 or len(text) <= max_chars:
        return text, "", 0
    head_len = max_chars // 2
    tail_len = max_chars - head_len
    head = text[:head_len]
    tail = text[len(text) - tail_len :] if tail_len else ""
    elided = len(text) - head_len - tail_len
    return head, tail, elided


def elide_middle(text: str, max_chars: int) -> str:
    """``text`` cut to a head and a tail around an omission marker; unchanged when it fits."""
    head, tail, elided = truncate_middle(text, max_chars)
    if not elided:
        return text
    return f"{head}\n[… {elided:,} characters omitted …]\n{tail}"
