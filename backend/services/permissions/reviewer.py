"""The model stage's vocabulary: what a reviewer is given, and what it answers.

What reaches a reviewer is what the structural judge would not vouch for (``judge.py``).
The verdict has three axes because "how bad is the act" and "did the operator want it" are
independent facts. ``risk`` is a property of the act, ``authorization`` of the
conversation, and ``correctness`` notes an act aimed at the wrong thing. They are combined
in ``decide.py``, and the passing scores are never shown to the model.

**The transcript excludes tool results**, and with them everything a file, page or MCP
server wrote, so a reviewer cannot be talked into approving the very call some untrusted
text asked for. That covers a compaction summary, which is user-shaped but built from tool
returns, and the per-turn context the chassis appends to the operator's prompt, which
includes the model's own task list.

A reviewer reports every failure as :class:`ReviewUnavailable` and never as a lenient
verdict; ``decide.py`` turns it into a park.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol

from pydantic import BaseModel, Field
from pydantic_ai.messages import ModelMessage, ModelRequest, TextPart, UserPromptPart

from core.compaction_sections import is_checkpoint
from core.untrusted import new_nonce
from services.permissions.capability import Capability

#: How much of the thread the reviewer reads, counted from the end in prose entries rather
#: than messages: a tool round trip is two messages and would crowd out what was said.
TRANSCRIPT_ENTRIES = 12

#: Per-message cap, so one pasted file cannot crowd out the turns around it.
MESSAGE_CHARS = 2_000

type Risk = Literal["low", "high", "too_destructive"]

#: Whether the operator asked for an act, as the reviewer read the thread.
type Authorization = Literal["explicitly_no", "neutral", "explicitly_yes"]

#: How far the operator's standing conversation grant reaches over the call being ruled on:
#: none, the one act it was ticked under, or everything the tool does in the thread. Named
#: here because the grant store (``services/approval_grants.py``) sits above this package.
type GrantWidth = Literal["none", "command", "tool"]

#: Who approved an earlier call in the conversation. Only ``operator`` and ``grant`` are
#: the operator's word; ``review`` is an earlier Auto ruling, which authorizes nothing.
type ApprovedBy = Literal["operator", "grant", "review"]


class ReviewVerdict(BaseModel):
    """One reviewer's reading, on three axes."""

    #: ``too_destructive`` is unrecoverable; only a whole-tool grant clears it (``decide.py``).
    risk: Risk = "high"
    #: Whether the operator asked for it. Only the operator's own messages count.
    authorization: Authorization = "neutral"
    #: What about the act does not match the request, in a sentence. None ⇒ it matches.
    correctness: str | None = Field(default=None, max_length=400)


@dataclass(frozen=True)
class ScoredVerdict:
    verdict: ReviewVerdict
    #: The raw numbers the verdict was read from, for the review row.
    scores: Mapping[str, float]


@dataclass(frozen=True)
class ReviewUnavailable:
    """The review could not be made. ``detail`` names what failed, for the row."""

    detail: str


@dataclass(frozen=True)
class TranscriptEntry:
    """One turn of the conversation as the reviewer may read it.

    Role and text are kept apart and rendered as JSON fields, so a message's text cannot
    claim the operator's role.
    """

    role: Literal["operator", "assistant"]
    text: str


@dataclass(frozen=True)
class PriorApproval:
    """A call approved earlier in the conversation (``services/approval_ledger.py``)."""

    tool_name: str
    #: Model-authored, so it is fenced wherever it is shown to a model.
    summary: str
    approved_by: ApprovedBy


@dataclass(frozen=True)
class ReviewRequest:
    """What a reviewer is given: an action's worst case, and the thread that led to it."""

    capability: Capability
    #: Already filtered to user and assistant prose (:func:`review_transcript`). Empty is
    #: legitimate: a stateless turn has no thread.
    transcript: Sequence[TranscriptEntry] = ()
    #: The conversation's approved calls, oldest first.
    approved_calls: Sequence[PriorApproval] = ()
    #: The fence token. A batch of reviews over one transcript shares it so their shared
    #: prefix is byte-identical and cacheable; it is never shared across turns or threads.
    nonce: str = field(default_factory=new_nonce)


#: The stage as a function, so a test can substitute one without a model at all.
type Reviewer = Callable[[ReviewRequest], Awaitable[ScoredVerdict | ReviewUnavailable]]


class TurnBoundary(Protocol):
    """Where the current turn's own messages begin (``agent.history.TurnStart``), typed
    structurally because ``services`` sits below ``agent``."""

    def slice(self, messages: list[ModelMessage]) -> list[ModelMessage]:
        """``messages`` from the turn's boundary on."""
        ...  # pragma: no cover — a Protocol body, never executed


def review_transcript(
    messages: Sequence[ModelMessage],
    *,
    turn_start: TurnBoundary | None = None,
    limit: int = TRANSCRIPT_ENTRIES,
    chars: int = MESSAGE_CHARS,
) -> tuple[TranscriptEntry, ...]:
    """The thread as the reviewer may see it: the operator's requests and the assistant's
    prose, in order, and nothing else.

    Dropped: tool returns (the injection vector), tool calls (the judged one is described
    by its capability), thinking (the model's own argument for the act), and compaction
    summaries (user-shaped, but built from tool returns).

    The window is the last ``limit`` entries, plus the operator's request(s) that opened
    this turn, which a window counted from the end loses first. An opening entry the window
    dropped goes back in front, since it is older than everything in the window; one the
    window still holds keeps its place, so a superseded instruction never reads as the
    latest.
    """
    entries = _prose(messages, chars)
    opening = (
        tuple(
            entry
            for entry in _prose(turn_start.slice(list(messages)), chars)
            if entry.role == "operator"
        )
        if turn_start is not None
        else ()
    )
    window = entries[-limit:] if limit > 0 else ()
    kept: dict[TranscriptEntry, None] = dict.fromkeys(
        entry for entry in opening if entry not in window
    )
    kept.update(dict.fromkeys(window))
    return tuple(kept)


def _prose(messages: Sequence[ModelMessage], chars: int) -> tuple[TranscriptEntry, ...]:
    """Every message that carries somebody's own words, as one entry each, in order."""
    entries: list[TranscriptEntry] = []
    for message in messages:
        if isinstance(message, ModelRequest):
            text = "\n".join(
                _prompt_text(part)
                for part in message.parts
                if isinstance(part, UserPromptPart) and not _is_compaction_summary(part)
            ).strip()
            if text:
                entries.append(TranscriptEntry("operator", text[:chars]))
        else:
            text = "\n".join(
                part.content for part in message.parts if isinstance(part, TextPart)
            ).strip()
            if text:
                entries.append(TranscriptEntry("assistant", text[:chars]))
    return tuple(entries)


def _is_compaction_summary(part: UserPromptPart) -> bool:
    """Whether this user-shaped message is a compaction checkpoint, by the opening tag the
    compaction writes. An operator who types that tag loses the message from review."""
    return is_checkpoint(_prompt_text(part))


def _prompt_text(part: UserPromptPart) -> str:
    """The operator's own words in a user part, and nothing the chassis appended.

    Everything the prelude adds (attachment markers, images, the per-turn context with the
    model's own task list) follows the prompt, so only position 0 is read. Joining the list
    would put model-authored text under the operator's role.
    """
    if isinstance(part.content, str):
        return part.content
    first = part.content[0] if part.content else ""
    return first if isinstance(first, str) else ""
