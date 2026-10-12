"""Conversation-level events: titling, linking, and compaction (fold) progress."""

from __future__ import annotations

from typing import Literal

from pydantic import computed_field

from core.compaction_sections import SummarySection, summary_sections

from .base import _Body


class ConversationTitled(_Body):
    """The chassis named a freshly-created conversation from its first exchange,
    so the operator never has to. The title is persisted too; the frontend reveals
    it with a typing animation. Emitted mid-run (before ``run.ended``) so a still-
    open stream carries it. Additive to v1; no bump."""

    type: Literal["conversation.titled"] = "conversation.titled"
    conversation_id: str
    title: str


#: Why a fold happened. ``threshold`` is the prelude's projected-pressure trigger,
#: ``overflow`` the in-turn recovery after a provider refused an over-long request, and
#: ``manual`` the operator pressing "compact now". One vocabulary for both compaction
#: events, so a client that learns the word on one frame reads it on the other.
CompactionReason = Literal["threshold", "overflow", "manual"]


class CompactionStarted(_Body):
    """A fold is about to be summarized — emitted *before* the summarizer call, which is
    the one part of compaction that takes real time (a whole model pass over the thread).

    Without it the operator watches a thread sit silent for as long as the summary takes
    with nothing said, and the only frame that ever mentioned compaction was the one that
    announced it as already done. It also refreshes the inactivity watchdog (every
    ``Run.emit`` touches the activity clock) — the fold itself then holds the watchdog open
    with ``Run.keepalive``, so a long summary can't be read as a stalled run. Additive to
    v1; no bump."""

    type: Literal["compaction.started"] = "compaction.started"
    conversation_id: str
    reason: CompactionReason
    # What the fold is about to cover: how many messages, and their coarse
    # `estimate_tokens` size — the same proxy `conversation.compacted` reports against, so
    # the "before" figure the progress block shows is the one the divider settles on.
    messages: int
    tokens_estimate: int


class CompactionDelta(_Body):
    """A piece of the summary as the summarizer writes it.

    ``compaction.started`` says a fold has begun and ``conversation.compacted`` says it is
    done; between them sits the part that actually takes the time, and until this event
    there was nothing in it. On a local endpoint that is tens of seconds of a throbber with
    a count beside it, which tells the operator that something is happening and never what.

    **This is the model's working, not the checkpoint.** The text arrives raw: not stripped
    of a leaked ``<think>`` block, not merged with the anchors a previous fold carried
    forward, and not fenced. Those are done to the settled summary, and two of them cannot
    be done to a fragment at all. So a client renders these as a live view that is replaced
    when ``conversation.compacted`` lands with the real thing — never stores one, and never
    treats one as the summary the model will actually read."""

    type: Literal["compaction.delta"] = "compaction.delta"
    conversation_id: str
    text: str


class CompactionFailed(_Body):
    """A fold that began did not land — the closing frame for a ``compaction.started`` that
    will never be followed by ``conversation.compacted``.

    Without it a client holding a live "compacting" section, filled with the streamed
    summary, had nothing to tell it the work was abandoned: the section sat there until the
    run ended and a reload showed no trace of it, because the backend had never recorded a
    compaction. On this frame the client drops the section (and its streamed text) and may
    show ``message``. Emitted for every failure of a fold that had something to do — never
    for a fold that found nothing to fold, which also emits no ``compaction.started``.
    ``message`` is a short operator-readable cause, not a log line. Additive to v1; no
    bump."""

    type: Literal["compaction.failed"] = "compaction.failed"
    conversation_id: str
    reason: CompactionReason
    message: str


class ConversationCompacted(_Body):
    """The thread's earlier turns were folded into a summary before this turn ran,
    because its context footprint had reached the operator's threshold. Nothing was
    deleted — the transcript keeps every turn; this marks where the *model's* replayed
    view narrows to ``summary`` plus the turns after it. Emitted mid-run (before the
    answer streams) so a live client can drop the divider in as it happens, and
    persisted as its own message so a reload renders the same thing. Additive to v1;
    no bump."""

    type: Literal["conversation.compacted"] = "conversation.compacted"
    conversation_id: str
    # Why the fold happened, in the same words `compaction.started` used. Defaulted so a
    # replayed frame from before this field existed still parses as what it was.
    reason: CompactionReason = "threshold"
    # The checkpoint message the summary is stored on — the node the client renders
    # the divider against, and the same id a cold read returns.
    message_id: str
    summary: str
    # How many messages the summary stands in for, so the divider can say so without
    # the client counting anything itself.
    messages_compacted: int
    # What the fold cost, in coarse tokens: `tokens_before` over the messages that were
    # folded, `tokens_after` over the summary that replaced them. Both are the same
    # `estimate_tokens` text-only proxy the compaction trigger itself measures with — not
    # a provider's usage report — so a client should render them as approximate ("~62k →
    # ~4k"), never as billing figures. `services/conversation_view.py` recomputes the
    # identical three values for the cold-read compaction row, so a live divider and the
    # one a reload draws say the same thing.
    tokens_before: int = 0
    tokens_after: int = 0
    # The rendered turn the divider follows. A live client addresses turns, not tree
    # nodes, so the backend resolves the position rather than leaving the client to
    # approximate it and land somewhere a reload disagrees with. Null => append.
    after_message_id: str | None = None

    @computed_field
    @property
    def sections(self) -> list[SummarySection]:
        """``summary`` split into the roster sections a divider renders.

        Derived rather than passed in, for the same reason :attr:`RunMetrics.context` is: a
        second field carrying a parse of a field already on the frame is a second thing to
        keep in step, and the two disagreeing is a bug nobody sees until a checkpoint reads
        wrong. ``services/conversation_view.py`` calls the *same* function on the *same*
        stored text, so the divider a live client draws and the one a reload draws are the
        same divider."""
        return summary_sections(self.summary)
