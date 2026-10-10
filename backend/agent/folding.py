"""When a *turn* folds the thread, how the fold is announced, and what it does to the
turn's persistence boundary.

``summarize.py`` is the fold itself — what gets read and what gets written. :func:`fold`
here is the layer above it that **every** trigger goes through: it announces the fold with
the ``compaction.started``/``conversation.compacted`` pair (or ``compaction.failed`` when it
does not land) and hands back what happened.
The operator's own "compact now" reaches it from ``routes/conversations.py``; the two
turn-shaped triggers reach it through the wrappers below.

The rest of this module is the part only a *turn* can know: the projected check that fires
a fold between turns, the mid-turn recovery that fires one after a provider refused the
request, and the rebuild of the history the retried request is sent against with the
``TurnStart`` boundary moved to match. A fold inside a turn moves the line between what the
thread already had and what this turn is about to persist; nowhere else has to think about
that, which is why it is here and not there.

Nothing here reads settings for itself: the value rides on the ``CompactionContext`` the
turn built once, so every reading of the thread's size in one turn is against one object.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic_ai import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    UserPromptPart,
)

from runs import (
    CompactionDelta,
    CompactionFailed,
    CompactionReason,
    CompactionStarted,
    ConversationCompacted,
    Run,
    TurnOverhead,
)
from services.conversation_view import estimate_tokens

from .compaction_context import CompactionContext
from .history import (
    TurnStart,
    drop_dangling_tool_calls,
    merge_consecutive_requests,
)
from .metrics import post_fold_metrics
from .summarize import (
    CompactionOutcome,
    FoldFailed,
    FoldResult,
    compact_conversation,
    should_compact,
)

logger = logging.getLogger(__name__)


def incoming_request(
    user_prompt: str | list[Any] | None, extra: list[str]
) -> ModelRequest | None:
    """This turn's own new content as one request, for measuring it — the operator's
    message with its attachment markers, plus any per-turn context that isn't already
    inside it (a regenerate has no fresh prompt, so its context is the only new part).

    ``None`` when the turn adds nothing measurable, which is a plain regenerate with no
    context providers wired. Binary parts ride along untouched; the estimator ignores
    them by design."""
    parts: list[Any] = []
    if user_prompt is not None:
        parts.extend(user_prompt if isinstance(user_prompt, list) else [user_prompt])
    parts.extend(extra)
    return ModelRequest(parts=[UserPromptPart(content=parts)]) if parts else None


async def fold(run: Run, ctx: CompactionContext, *, reason: CompactionReason) -> FoldResult:
    """Run one compaction and announce it on ``run``, answering with what happened.

    **The one path every fold takes**, whichever trigger fired — the prelude's projected
    threshold, the mid-turn overflow recovery, or the operator's own button — so no trigger
    can drift on what is emitted, in what order, or under what policy. A trigger decides
    only *whether* to fold and what ``reason`` to stamp; everything past that point is
    here.

    The summary is written by ``ctx.agent`` — the turn's own agent, continuing its own
    conversation — so every trigger has to hand over the agent and deps a turn would run
    with; the operator's button composes them the way a chat turn does.

    **This never raises**, and that is a property of the function rather than a favour to
    one caller: a fold is an efficiency measure, and a turn that dies for one is worse off
    than a turn that meets the model's real ceiling. What a failure *means*, though, is the
    caller's — an automatic trigger carries on, while the operator's own button has to say
    so — which is why the outcome comes back as a value instead of being swallowed here.

    ``compaction.started`` goes out from inside the plan callback rather than before it, so
    it is emitted only once there is genuinely something to fold and can state what.

    **A fold has no time limit.** The summarizer runs until it finishes, and the whole
    fold sits inside ``Run.keepalive`` so the inactivity watchdog cannot end it — the
    run's own wall clock, when one is set, is the only bound left."""
    try:
        # The summarizer has no time limit, so the run's inactivity watchdog must not stand
        # in for one: a local model prefilling a long thread can go minutes before its
        # first token, and a model thinking before it writes streams no event at all (its
        # reasoning is not part of the summary) and looks idle while it works.
        async with run.keepalive():
            result = await compact_conversation(
                ctx.store,
                ctx.conversation_id,
                agent=ctx.agent,
                deps=ctx.deps,
                reason=reason,
                on_plan=lambda plan: run.emit(
                    CompactionStarted(
                        conversation_id=ctx.conversation_id,
                        reason=reason,
                        messages=len(plan.messages),
                        tokens_estimate=estimate_tokens(plan.messages),
                    )
                ),
                # The summary as it is written, so the operator watches it arrive.
                on_delta=lambda text: run.emit(
                    CompactionDelta(conversation_id=ctx.conversation_id, text=text)
                ),
            )
    except Exception:  # noqa: BLE001 — a fold must never take its caller down with it
        logger.warning("compaction failed for %s", ctx.conversation_id, exc_info=True)
        return _failed(run, ctx, reason, FoldFailed("error"))
    if isinstance(result, FoldFailed):
        return _failed(run, ctx, reason, result)
    if isinstance(result, CompactionOutcome):
        run.emit(
            ConversationCompacted(
                conversation_id=ctx.conversation_id,
                reason=reason,
                message_id=result.message_id,
                summary=result.summary,
                messages_compacted=result.messages_compacted,
                tokens_before=result.tokens_before,
                tokens_after=result.tokens_after,
                after_message_id=result.after_message_id,
            )
        )
    return result


def _failed(
    run: Run, ctx: CompactionContext, reason: CompactionReason, failure: FoldFailed
) -> FoldFailed:
    """Close a fold that did not land with ``compaction.failed``, then hand the failure back.

    The frame is what ends the client's live "compacting" section: the backend recorded no
    checkpoint, so without it that section would sit with its streamed text until the run
    ended and vanish on a reload, showing a compaction the backend never had."""
    run.emit(
        CompactionFailed(
            conversation_id=ctx.conversation_id, reason=reason, message=failure.message
        )
    )
    return failure


async def _fold_replay(
    run: Run, ctx: CompactionContext, *, reason: CompactionReason
) -> list[ModelMessage] | None:
    """A turn's reading of :func:`fold`: the replay it leaves behind, or ``None`` when
    nothing folded.

    Re-reading ``model_history`` is the turn-shaped half — the route has no replay to
    rebuild — so it lives here rather than inside the shared path, and all three of the
    non-outcome cases collapse to the same ``None`` because a turn does the same thing for
    each of them: carry on against the history it already had."""
    result = await fold(run, ctx, reason=reason)
    if not isinstance(result, CompactionOutcome):
        return None
    return await ctx.store.model_history(ctx.conversation_id)


async def maybe_compact(
    run: Run,
    ctx: CompactionContext | None,
    history: list[ModelMessage],
    *,
    overhead: TurnOverhead | None,
    incoming_tokens: int,
    context_window: int | None,
) -> tuple[list[ModelMessage], bool]:
    """Fold this conversation's older turns when the turn *about to run* would reach the
    operator's share of the model's context window, returning the history to replay and
    whether anything folded.

    The trigger is projected, not retrospective: ``incoming_tokens`` is this turn's own
    prompt, its attachments and the per-turn context appended to it, all of which are
    resolved before this runs precisely so they can be counted here. A thread at 70% that
    is about to be handed a 15% prompt is a thread that needs folding now, and measuring
    the history alone could not see that.

    Returns ``history`` unchanged whenever compaction is off, unmeasurable (no declared
    window), not yet due, has nothing left to fold, or the summarizer failed."""
    if ctx is None or ctx.policy is None or not ctx.policy.enabled:
        return history, False
    if not should_compact(
        history,
        context_window,
        ctx.policy.threshold,
        overhead=overhead,
        incoming_tokens=incoming_tokens,
        settings=ctx.settings,
    ):
        return history, False
    folded = await _fold_replay(run, ctx, reason="threshold")
    return (history, False) if folded is None else (folded, True)


def _without_empty_tail(messages: list[ModelMessage]) -> list[ModelMessage]:
    """Drop the empty response the streaming path leaves behind when a request fails.

    Pydantic AI appends a placeholder ``ModelResponse`` to the history before it starts
    streaming into it, so a request the provider refuses outright leaves a partless
    response as the tail. It is not a message — nothing was generated — and leaving it
    there would end the rebuilt history on a response, which is not a shape a turn can be
    resumed from."""
    while messages and isinstance(messages[-1], ModelResponse) and not messages[-1].parts:
        messages = messages[:-1]
    return messages


async def compact_and_retry(
    run: Run,
    ctx: CompactionContext,
    *,
    partial_history: list[ModelMessage],
    turn_start: TurnStart,
) -> list[ModelMessage] | None:
    """Fold the thread mid-turn and rebuild the history the failed request will be re-sent
    against, or ``None`` when nothing folded.

    The turn's own messages are not recorded yet, so the tree's leaf is still the previous
    tip and the checkpoint lands exactly where the prelude's fold would have put it. What
    the model replays afterwards is the folded history *plus this turn so far* — the
    operator's prompt, the tool calls it has already made and their results, none of which
    is in the tree yet and all of which the retried request depends on.

    The order below is load-bearing:

    - the dangling-call strip runs on the folded history alone, because this turn's own
      trailing call is answered by the very request being retried;
    - the merge runs over the **concatenation**, because the library merges consecutive
      requests when it cleans a history it is resuming, and a checkpoint hoisted in front
      of a turn that opens on a user prompt is exactly that shape. Pre-merging is what
      keeps ``all_messages()`` the length we measured our index against;
    - the boundary is then read off the two sides' *shapes*, because that merge can leave
      the turn beginning inside a message the folded history also owns. Half a message is
      still a boundary: without recording how much of it is the turn's own, the persist
      re-records the checkpoint summary — and whatever else the replay put in front of the
      prompt, the reinjected brief included — as new operator messages.

    What gets summarized is the thread's *recorded* turns (``compaction_plan``), not the
    request that overran — this turn is not in the tree yet — so the summary request is
    the smaller of the two. If even that overruns the window, the fold fails like any other
    and the turn stops with the context notice; nothing is trimmed to make it fit.

    An operator who switched compaction off for this thread is not overruled by an
    overflow: they get the stop, and the **Compact and retry** it offers, which is the
    same fold under their own hand.
    """
    if ctx.policy is None or not ctx.policy.enabled:
        return None
    folded = await _fold_replay(run, ctx, reason="overflow")
    if folded is None:
        return None
    # Merged per side first, so the only merge the concatenation can still perform is the
    # one at the boundary — which is the one the index has to know about. Reading it off
    # the two shapes is exact; reading it off the lengths would mistake a merge *inside*
    # either side for a collapsed boundary.
    head = merge_consecutive_requests(drop_dangling_tool_calls(folded))
    turn_slice = merge_consecutive_requests(
        _without_empty_tail(partial_history[turn_start.index :])
    )
    collapsed = (
        bool(head)
        and bool(turn_slice)
        and isinstance(head[-1], ModelRequest)
        and isinstance(turn_slice[0], ModelRequest)
    )
    rebuilt = merge_consecutive_requests([*head, *turn_slice])
    turn_start.index = len(head) - 1 if collapsed else len(head)
    turn_start.parts = len(turn_slice[0].parts) if collapsed else 0
    # Nothing in the rebuilt replay was measured against the thread as it now stands — this
    # turn's own responses included, since they reported their prompt size before the fold.
    # So the boundary is the whole replay: the frame below reads the estimate, and the first
    # believable reported figure is the one the retry itself comes back with.
    # And it goes out *now*, not when the retry answers. A fold that frees half the thread
    # and then overruns again would otherwise leave the gauge pinned at its pre-fold figure
    # for good — the operator watching a compaction that visibly changed nothing.
    run.emit(post_fold_metrics(run, rebuilt, settings=ctx.settings))
    return rebuilt
