"""The context gauge and the room check — the footprint arithmetic a turn is measured by.

Two pure readings over a message list. :func:`turn_metrics` is the thread's cumulative
readout, emitted live as each response lands and stashed as the run's terminal metrics;
:func:`no_room_for` is the yes/no the verifier asks before spending a second full pass at
the turn's peak pressure. They sit together because they are the same arithmetic seen from
two sides — what the replay costs against the window it has to fit in — and apart from
``engine.py`` because the turn, the two folds and the verifier all read them, and none of
those is the reason this file would change.

Neither reads settings for itself: the value arrives as a keyword, so the caller that
already resolved one settings object measures every frame of a turn against it.
"""

from __future__ import annotations

import logging

from pydantic_ai import ModelMessage, ModelRequest, UserPromptPart

from core.config import Settings
from runs import LastRequestUsage, Run, RunMetrics, total_timings
from services.context_budget import compose
from services.conversation_view import estimate_footprint, estimate_tokens
from services.conversations import (
    conversation_totals,
    footprint_or_estimate,
    last_request_usage,
)

logger = logging.getLogger(__name__)


def turn_metrics(run: Run, messages: list[ModelMessage], *, settings: Settings) -> RunMetrics:
    """The thread's cumulative readout, counted off ``messages`` — the full replayed
    history, not just this run's own additions.

    **Derived, not accumulated.** ``messages`` is everything on the active path, and
    each stored response carries the usage the provider reported for it, so every count
    and token here is a fresh sum over the path. That is what makes the figures survive
    a reload, a rewind and a version switch without a counter to keep in step: the same
    ``conversation_totals`` runs on a cold load and produces the same answer. It is also
    why this no longer takes a ``base``/``usage`` pair — the run's own ``RunUsage``
    covers only the current run, and adding it to a path-derived total would count this
    turn twice.

    Time is the exception, and the only thing still carried on the Run: it isn't in the
    message blobs. ``run.prior_timings`` holds the persisted total for the turns before
    this one, and ``run.timer`` holds this run's own, so the two add.

    ``context_used`` is the *footprint* — the last response's prompt+generation, not the
    path's summed tokens — so a long thread doesn't overstate fullness. Built in one
    place so the live per-step frames (the context gauge) and the stashed terminal
    metrics never diverge.

    **Fold-aware.** A response that landed *before* this run's most recent compaction
    reported its prompt size against a history that no longer exists, so the footprint is
    read only from ``run.fold_boundary`` onward. Until the first post-fold response lands
    there is nothing measured to read, and the estimate stands in — which is the whole
    point: without it the gauge would sit pinned at the pre-fold figure through the very
    turn the fold made room for, and the operator would watch a compaction change nothing.
    ``last_request_usage`` still reads the whole path: which model spoke last, and what its
    cache did, are facts about a request, not about the current replay."""
    counts = conversation_totals(messages)
    timings = run.prior_timings + total_timings(run.timer.responses)
    # The boundary decides which *reported* figures may still be believed, not what the
    # estimate covers: the estimate is always over the whole replay, because the whole
    # replay is what the next request carries. Same helper the cold read calls, so a
    # thread reopened after a run reports the figure the live frames did.
    footprint = footprint_or_estimate(
        messages,
        run.context_overhead,
        fallback_overhead_tokens=settings.context_overhead_fallback_tokens,
        reported_from=messages[min(run.fold_boundary, len(messages)) :],
    )
    last_request = _with_prefill(last_request_usage(messages), run, footprint)
    _log_prefill_diagnostic(run, last_request)
    return RunMetrics(
        steps=counts.steps,
        tool_calls=counts.tool_calls,
        turns=counts.turns,
        input_tokens=counts.input_tokens,
        output_tokens=counts.output_tokens,
        cache_read_tokens=counts.cache_read_tokens,
        # A thread whose responses all predate the stopwatch reports no time rather
        # than none-elapsed — the same absent-not-zero rule the token counts follow.
        llm_ms=timings.llm_ms or None,
        tool_ms=timings.tool_ms or None,
        ttft_ms_total=timings.ttft_ms_total or None,
        ttft_samples=timings.ttft_samples,
        context_window=run.context_window,
        context_used=footprint,
        context_thresholds=run.context_thresholds,
        context_fold=run.context_fold,
        # The split of that footprint. Scaled to the provider's own total, so the parts
        # always add up to the figure beside them even though each is an estimate.
        context_parts=compose(footprint, run.context_overhead, messages),
        # The last request on its own — read off the same path as everything else, so a
        # reload reports the route and the cache figures the live turn did.
        last_request=last_request,
    )


def post_fold_metrics(run: Run, replay: list[ModelMessage], *, settings: Settings) -> RunMetrics:
    """The readout for a thread that has just been folded: ``replay`` is the whole history
    the next request carries, and none of it was measured against the thread as it now
    stands, so the boundary is the whole replay and the footprint reads the estimate.

    The one place that rule is applied, because three callers need it identically — the
    prelude's threshold fold, the in-turn overflow recovery, and the operator's own fold,
    which has no turn to take the figure from but must still leave the run with a real
    terminal frame rather than an empty one."""
    run.fold_boundary = len(replay)
    return turn_metrics(run, replay, settings=settings)


def _with_prefill(
    last_request: LastRequestUsage | None, run: Run, footprint: int | None
) -> LastRequestUsage | None:
    """Attach the prompt size and time-to-first-token the apparent prefill rate is over.

    Added here rather than inside ``last_request_usage`` because neither figure is in the
    message history: the timings are on the run's own stopwatch, and the prompt size is the
    measured-or-estimated footprint this function just computed. That is also why this is a
    *live* enrichment — a cold load reports the route and the cache figures a reload can
    derive, and no prefill rate, which is honest: nothing persisted says how long that
    request waited.

    The numerator is the footprint **less the response's own output**, because the footprint
    is prompt plus generation and only the prompt is prefilled. Where the provider reported
    no output count the subtraction is skipped and the figure runs one response's generation
    high — bounded, and the reason the rate is labelled apparent rather than exact.
    """
    if last_request is None or footprint is None or not run.timer.responses:
        return last_request
    timing = run.timer.responses[-1]
    if timing.ttft_ms is None:
        return last_request
    prompt = max(0, footprint - (last_request.output_tokens or 0))
    return last_request.model_copy(update={"prefill_tokens": prompt, "prefill_ms": timing.ttft_ms})


def _log_prefill_diagnostic(run: Run, last_request: LastRequestUsage | None) -> None:
    """One line per response correlating the wait with what moved in the request's prefix.

    The whole point of the prefix watch is that a ``ttft_ms`` spike and its cause have to be
    readable *on the same line* — a verdict in one log and a duration in another is a
    correlation the reader has to do by hand, at which point they won't. So this puts the
    route, the prompt size, the provider's cache figure, the wait, the apparent rate and the
    prefix verdict together, with the brief/schema split that says how much of the head there
    was to invalidate in the first place.

    At ``debug`` deliberately, and gated by nothing else. Logging levels are the mechanism
    that already exists for "I am investigating something"; a settings field would make an
    instrument into an installation property, and would have to be threaded down to a module
    whose defining constraint is that it reads no settings for itself.

    **How to read it, and the one case that needs two fields together.** ``head=`` names what
    moved in the prefix; ``prefill_tok_s`` says whether anything was reused. The pair
    distinguishes the two causes that look identical from a slow turn alone:

    - ``head=`` naming a block, with a rate near the hardware's true prefill speed: **we**
      invalidated the prefix, and the block named is where to look.
    - ``head=none`` with a rate near that same speed: the prefix was byte-stable and the
      *engine* still re-read it. Nothing in this codebase can fix that — it is a cache the
      server dropped, most often because the model was unloaded and reloaded between turns
      (LM Studio scopes its prompt cache to the model load, so a JIT unload discards it), or
      because something else on the endpoint evicted it.
    - ``head=none`` with an implausibly high rate: the prefix was reused. That is the
      intended state, and the rate is meaningless because there was no prefill to measure.

    The absolute rate is what makes the second case legible, which is why it is on this line
    rather than left to the gauge.
    """
    if not logger.isEnabledFor(logging.DEBUG):
        return
    overhead = run.context_overhead
    verdict = run.prefix_verdict
    logger.debug(
        "prefix run=%s route=%s prompt=%s cached=%s ttft_ms=%s prefill_tok_s=%s "
        "brief=%s schemas=%s %s",
        run.id,
        last_request.route if last_request else None,
        last_request.prefill_tokens if last_request else None,
        last_request.cache_read_tokens if last_request else None,
        last_request.prefill_ms if last_request else None,
        round(last_request.prefill_tokens_per_second)
        if last_request and last_request.prefill_tokens_per_second
        else None,
        overhead.system if overhead else None,
        overhead.tools if overhead else None,
        verdict.summary() if verdict else "head=unwatched",
    )


def no_room_for(
    run: Run,
    messages: list[ModelMessage],
    nudge: str,
    *,
    threshold: float | None,
    settings: Settings,
) -> bool:
    """Whether replaying ``messages`` plus ``nudge`` would already be over the operator's
    share of the window. ``False`` whenever there is no window or no threshold to measure
    against — the same rule the compaction trigger follows, for the same reason."""
    if not run.context_window or not threshold or threshold <= 0:
        return False
    projected = estimate_footprint(
        messages,
        run.context_overhead,
        fallback_overhead_tokens=settings.context_overhead_fallback_tokens,
    ) + estimate_tokens([ModelRequest(parts=[UserPromptPart(content=nudge)])])
    return projected >= run.context_window * threshold
