"""Conversation compaction — folding a thread's older turns into a summary its own model
writes.

**The product's only context reduction** (`AE-5.4`, `CHAT-4`), and the only one that ever
existed for a good reason: it fires on *measured pressure*, when a thread's footprint has
actually reached the operator's share of the model's context window. Everything else that
used to shrink content — digesting prior-turn tool results, capping an attachment's inline
text, trimming a code run's stdout — fired unconditionally, on turns under no pressure at
all, and has been removed. A large tool result now rides into context whole; the run's own
context-overflow stop is what catches a pathological turn.

Three properties make this safe to run automatically:

- **It never fires underneath reasoning in flight.** The trigger sits in the orchestrator
  prelude, before the agent runs, and measures what the turn *about to run* will cost. The
  one exception is the recovery: when a provider refuses a request as over-long, the turn
  folds once and re-sends that same request — between two requests, with nothing in
  flight, which is the only mid-turn moment at which folding is safe.
- **Nothing is destroyed.** The summary is appended as a new checkpoint node; the turns it
  covers stay in the tree, in the operator's transcript, and in cross-chat search. Only
  what is *re-sent to the model* narrows (``ConversationStore.model_history``). A rewind
  above the checkpoint restores the full replay for free.
- **It is still not a safety net.** A fold gets *one* attempt at an overflow. A request
  that is too big after it is too big for a reason folding cannot reach, and the run stops
  with a context notice rather than re-folding a thread down to nothing. Compaction lowers
  the pressure; it never silently drops content to force a fit.

How the summary is written, and how the text it produces is trusted afterwards, live next
door in :mod:`agent.compaction_summary`: the turn's own agent continues its own
conversation with one more message asking for the briefing, so the local engine reuses the
prefix it already holds, and what comes back has its anchors carried across folds verbatim
and its tool-sourced facts fenced on the way into the checkpoint. This module owns *when* a
thread folds and *what is recorded*.

**A fold keeps no retained tail.** It summarizes everything since the newest checkpoint —
that checkpoint's own summary included — and the new summary alone carries the thread
from there: nothing is replayed word for word beneath it. A single exchange that filled
the window folds whole; only a thread with nothing after its newest checkpoint has
nothing to fold.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from pydantic_ai import ModelMessage, ModelRequest, UserPromptPart

from core.config import Settings, get_settings
from prompts.compaction import COMPACT_PREAMBLE
from runs.events import CompactionReason
from runs.overhead import TurnOverhead
from services.conversation_view import estimate_footprint, estimate_tokens
from services.conversations import CompactionPlan, ConversationStore, context_footprint
from services.settings_store import (
    SettingsStore,
    get_auto_compact,
    resolve_compaction_enabled,
)
from tools import RunDeps

from .compaction_summary import (
    DeltaSink,
    SummaryCalledTool,
    carried_anchors,
    fence_tool_facts,
    merge_anchors,
    write_summary,
)
from .factory import TurnAgent
from .history import revealed_tools

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AutoCompactPolicy:
    """The effective conversation-compaction policy for one turn.

    Resolved at the route (operator default folded with the per-conversation override) and
    handed to the orchestrator, the same way the per-turn request limit is — so the engine
    never reads the settings store itself."""

    enabled: bool
    threshold: float


@dataclass(frozen=True)
class NothingToFold:
    """Nothing followed the thread's newest checkpoint, so no fold was attempted.

    Not a failure, and the distinction is the whole reason this is its own type: a fold
    that found nothing to do and a fold that tried and could not are the same ``None`` to
    a caller, and reporting the second as the first is what makes a broken button look
    like an empty conversation (``ConversationStore.compaction_plan``)."""


#: Why a fold that had work to do did not land. ``summarizer_empty`` is a model that
#: returned no text; ``tool_call`` is one that answered with a tool call it was told not to
#: make (never executed); ``leaf_moved`` is the active leaf shifting under a summary that
#: now describes a path the operator is not on; ``error`` is anything raised on the way,
#: the model's own failure included.
type FoldFailure = Literal["summarizer_empty", "tool_call", "leaf_moved", "error"]


#: What a fold that *had* work to do and did not land tells the operator. One sentence per
#: cause rather than one for all of them, because what they would do next differs: a
#: summarizer that wrote nothing is worth retrying, and a leaf that moved means the fold
#: described a path they have navigated away from.
_FOLD_FAILURE_MESSAGES: dict[FoldFailure, str] = {
    "summarizer_empty": "The fold did not land — the model returned no summary. Try again.",
    "tool_call": (
        "The fold did not land — the model answered with a tool call instead of the "
        "summary, and it was not run. Try again."
    ),
    "leaf_moved": (
        "The fold did not land — the conversation moved while it ran, so the summary "
        "described a path you are no longer on. Try again."
    ),
    "error": "The fold did not land. Try again, or check the logs for what failed.",
}


@dataclass(frozen=True)
class FoldFailed:
    """A fold that had something to do and did not land."""

    cause: FoldFailure

    @property
    def message(self) -> str:
        """The operator-readable sentence for ``cause`` — what the run's blocked detail and
        the ``compaction.failed`` frame both say, so the two never word it differently."""
        return _FOLD_FAILURE_MESSAGES[self.cause]


@dataclass(frozen=True)
class CompactionOutcome:
    """What a compaction actually folded, for the event the run emits."""

    message_id: str
    summary: str
    messages_compacted: int
    # What the fold cost, in coarse `estimate_tokens` terms: over the folded messages, and
    # over the summary that replaced them. The same proxy the trigger measures with, so
    # the divider's "~62k → ~4k" is consistent with the gauge the operator was watching.
    tokens_before: int
    tokens_after: int
    # The rendered turn the divider follows, so a live client places it where a reload will.
    after_message_id: str | None
    # What triggered this fold, echoed back from the caller. Carried on the outcome rather
    # than only on the caller's own event because it is also what was written onto the
    # checkpoint — one value, so the divider a reload draws names the same cause the live
    # one did.
    reason: CompactionReason = "threshold"


#: What one fold answers with. Three outcomes rather than one nullable value, because a
#: caller has to tell them apart: an automatic trigger treats all three alike (the turn
#: carries on), while the operator's own button has to say which happened — and telling
#: them apart used to mean asking the store for the plan a second time, purely to work out
#: what the ``None`` it had just been handed meant.
type FoldResult = CompactionOutcome | NothingToFold | FoldFailed


def build_auto_compact_policy(
    settings: Settings,
    *,
    enabled: bool | None = None,
    threshold: float | None = None,
) -> AutoCompactPolicy:
    """Resolve the effective policy from the config defaults, with optional operator
    overrides."""
    return AutoCompactPolicy(
        enabled=settings.auto_compact_enabled if enabled is None else enabled,
        threshold=settings.auto_compact_threshold if threshold is None else threshold,
    )


async def resolve_auto_compact_policy(
    store: SettingsStore, owner_id: str, *, override: bool | None = None
) -> AutoCompactPolicy:
    """The effective policy for one turn: the operator's stored preferences, with a
    conversation's on/off ``override`` folded in on top.

    The one place the precedence lives, so the interactive path and the scheduler's
    unattended one can't disagree about whether compaction is on."""
    stored = await get_auto_compact(store, owner_id)
    return build_auto_compact_policy(
        get_settings(),
        enabled=resolve_compaction_enabled(override, stored.enabled),
        threshold=stored.threshold,
    )


def should_compact(
    messages: list[ModelMessage],
    window: int | None,
    threshold: float,
    *,
    overhead: TurnOverhead | None = None,
    incoming_tokens: int = 0,
    settings: Settings | None = None,
) -> bool:
    """Whether the turn *about to run* would reach the operator's share of the window.

    **Projected, not retrospective.** The history's own footprint is what the last turn
    cost; the number that matters is what this turn will cost, which is that plus the
    operator's new prompt, its attachments and the per-turn context appended to it
    (``incoming_tokens``). Measuring only the history is why a threshold had to sit at 95%
    to be safe — it left the incoming turn to fit in whatever the previous one happened not
    to use.

    The two sources of the current size are taken at their **maximum**: the provider's own
    reported prompt size (exact, but absent on the local servers this workspace mostly
    talks to, and stale the moment anything is added), and the estimate over the messages
    plus this thread's measured brief + tool schemas. Whichever reads larger is the one
    that decides — under-reading here is what lets a thread walk into the overflow the fold
    exists to prevent.

    ``False`` when the endpoint declares no window: there is nothing to measure against,
    and compacting on a guess would fold a thread that was never under pressure."""
    if not window or threshold <= 0:
        return False
    cfg = settings or get_settings()
    reported = context_footprint(messages) or 0
    estimated = estimate_footprint(
        messages, overhead, fallback_overhead_tokens=cfg.context_overhead_fallback_tokens
    )
    return max(reported, estimated) + max(0, incoming_tokens) >= window * threshold


async def compact_conversation(
    store: ConversationStore,
    conversation_id: str,
    *,
    agent: TurnAgent,
    deps: RunDeps,
    reason: CompactionReason,
    on_plan: Callable[[CompactionPlan], None] | None = None,
    on_delta: DeltaSink | None = None,
) -> FoldResult:
    """Fold this conversation's older turns into a summary checkpoint, answering with what
    happened: the outcome, :class:`NothingToFold`, or :class:`FoldFailed`.

    The one path every trigger shares — the engine's projected threshold, the mid-turn
    overflow recovery, and the operator's own button — so they cannot drift on what gets
    folded or how it is recorded.

    **The three results are distinct types rather than one ``None``**, because they are
    three different things to have happened and only the caller knows which of them is
    worth reporting. A caller that cannot tell them apart has to re-derive the difference,
    which is a second measurement of a tree that may have moved in between.

    A **summarizer** failure is contained here (it becomes ``FoldFailed``), but a store
    failure is not: the operator pressing "compact now" should be told the write failed, not
    that there was nothing to fold. :func:`agent.folding.fold` wraps this so a turn never
    dies for a compaction it only wanted as an optimization.

    ``agent`` and ``deps`` are the turn's own: the summary is written by the model the
    thread runs on, continuing its own conversation (:mod:`agent.compaction_summary`).

    ``on_plan`` is called once the fold is known and *before* the summarizer runs — the one
    moment at which what is about to be folded can be announced, since the summarizer call
    is the seconds-long part. The engine emits ``compaction.started`` from it; a caller with
    nothing to announce passes nothing.

    ``reason`` is required rather than defaulted, and travels all the way onto the stored
    checkpoint. Each of the three callers knows which trigger it is — the threshold, the
    mid-turn overflow recovery, the operator's own button — and a default here would let a
    new one silently record the most common answer instead of its own."""
    plan = await store.compaction_plan(conversation_id)
    if plan is None:
        return NothingToFold()
    if on_plan is not None:
        on_plan(plan)
    summary = await summarize_history(agent, deps, plan.messages, on_delta=on_delta)
    if isinstance(summary, FoldFailed):
        return summary
    # Labelled on the way in, not on the way out: the stored text is what both the model
    # replays and the operator reads, and it needs to announce itself as a summary in both
    # places. The same framed text rides the event, so the divider a live client draws and
    # the one a reload draws are the same string.
    labelled = f"{COMPACT_PREAMBLE}\n\n{summary}"
    message_id = store.record_compaction(
        conversation_id,
        summary=labelled,
        through_id=plan.through_id,
        expected_leaf_id=plan.expected_leaf_id,
        reason=reason,
        # A fold must not quietly un-reveal a dormant group. The messages it replaces are
        # the only record that the model ever loaded the browser (or the mailbox), and the
        # library reads that record fresh on every request — so what the folded stretch
        # revealed is carried onto the checkpoint, and the thread keeps the tools it was
        # working with. Read off `plan.messages` rather than the whole thread: a *previous*
        # checkpoint's carried delta is inside this fold, so a second fold inherits the
        # first's without special-casing.
        revealed_tools=revealed_tools(plan.messages),
    )
    if message_id is None:
        # The active leaf moved while the summary was being written (a version switch or a
        # rewind — the conversation claim blocks runs, not navigation). The summary now
        # describes a path the operator isn't on, so drop it rather than graft it.
        logger.info("compaction for %s discarded: the active leaf moved", conversation_id)
        return FoldFailed("leaf_moved")
    return CompactionOutcome(
        message_id=message_id,
        summary=labelled,
        messages_compacted=len(plan.messages),
        # Measured over the same two things the projection re-measures on a cold read —
        # the folded messages, and the stored checkpoint text — so the live divider and
        # the reloaded one report the same numbers.
        tokens_before=estimate_tokens(plan.messages),
        tokens_after=estimate_tokens([ModelRequest(parts=[UserPromptPart(labelled)])]),
        after_message_id=plan.anchor_id,
        reason=reason,
    )


async def summarize_history(
    agent: TurnAgent,
    deps: RunDeps,
    messages: list[ModelMessage],
    *,
    on_delta: DeltaSink | None = None,
) -> str | FoldFailed:
    """Summarize a stretch of conversation into the briefing that will stand in for it, or
    say why no briefing was written.

    Written by the turn's own agent, against the replay it would be sent plus one message
    asking for the briefing (:func:`agent.compaction_summary.write_summary`) — so the model
    that did the work reads it in its own format, and a local engine serves the request
    from the prefix it already holds. There is no transcript, no chunking and no merge: a
    stretch the model could not read whole is a stretch it could not have been replaying
    either, and the overflow that would signal it fails this fold like any other error.

    **No time limit and no output cap.** The call runs until the model finishes, so the
    briefing is as long as the thread needs; the run's own watchdog is held open for the
    fold by :func:`agent.folding.fold`.

    Best-effort and isolated: a model error leaves the thread uncompacted rather than
    failing the turn it was about to make room for (``error``), a reply with no text is
    ``summarizer_empty``, and a reply that called a tool despite being told not to is
    ``tool_call`` — the call is never run."""
    try:
        summary = await write_summary(agent, deps, messages, on_delta=on_delta)
    except SummaryCalledTool:
        logger.warning("conversation compaction summary failed: the model called a tool")
        return FoldFailed("tool_call")
    except Exception as exc:  # noqa: BLE001 — compaction is best-effort, never fails a turn
        # CancelledError is not an Exception subclass, so a cancelled run still propagates
        # rather than degrading to "no summary".
        logger.warning("conversation compaction summary failed: %s", exc)
        return FoldFailed("error")
    if not summary:
        return FoldFailed("summarizer_empty")
    # Anchors are what a re-summarized summary loses first, so a second fold carries the
    # previous checkpoint's exact paths, ids and numbers across verbatim rather than asking
    # a model to restate them one more time.
    summary = merge_anchors(summary, carried_anchors(messages))
    # Fenced on the way out, because this text is stored as a user-shaped checkpoint the
    # main model replays as its own memory: the one section that repeats what a page or a
    # document said must stay marked as data.
    return fence_tool_facts(summary)
