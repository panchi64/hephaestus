"""One turn, driven to its end: an answer, a park, or a stop at a bound.

The loop around ``agent.iter()`` — the segment structure of a turn (the initial model
pass, then a continuation for every batch of deferred calls that settles without the
operator), the no-progress guard / usage accumulator / review budget they share, the live
gauge frame as each response lands, mid-run steering drained at both boundaries, and the
ways a turn stops that are *state* rather than errors: the first-token timeout, the loop
breaker, a context overflow, and a call that needs the operator.

It is its own file because it is the one thing both orchestrators and the verifier's
corrective re-attempt run: ``engine.py`` sequences a run around it and ``verify.py`` calls
it a second time, and neither of those is the reason this loop would change. The policy it
applies is elsewhere — ``gating.py`` rules on the deferred calls, ``parking.py`` builds the
continuation, ``folding.py`` recovers from an overflow, ``model_errors.py`` reads the
provider's failure — so what is left here is control flow.

Nothing here reads settings for itself: the caller resolves one settings object and passes
it in, so every bound and every frame of a turn is measured against the same values.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass, field, replace

import httpx
from pydantic_ai import (
    AgentRun,
    DeferredToolRequests,
    DeferredToolResults,
    ModelMessage,
    ModelRequest,
    ModelRequestNode,
    ModelResponse,
    RunUsage,
    UsageLimits,
    UserPromptPart,
)
from pydantic_ai.exceptions import ModelAPIError, ModelHTTPError
from pydantic_ai.messages import UserContent
from pydantic_ai.tools import DeferredToolApprovalResult

from core.config import Settings
from core.container import ServiceContainer
from core.exceptions import ModelLoadError
from runs import AnswerRetracted, LimitNotice, Run
from services.conversations import ConversationBinding, ConversationStore
from services.notifications import NotificationService
from services.permissions import ReviewBudget
from tools import NO_DISABLED_TOOLS, RunDeps

from .compaction_context import CompactionContext
from .factory import TurnAgent
from .folding import compact_and_retry
from .gating import settle_deferred
from .history import TurnStart
from .injected import injected_text
from .meta import FirstTokenTimeout, LoopBreaker, LoopDetected
from .metrics import turn_metrics
from .model_errors import (
    CONTEXT_OVERFLOW_AFTER_FOLD_DETAIL,
    CONTEXT_OVERFLOW_DETAIL,
    context_limit_message,
    is_context_overflow,
    is_transient,
    model_load_hint,
)
from .open_tasks import open_tasks_notice
from .parking import DEFAULT_BINDING, park_for_input
from .translate import stream_agent_run

# A shared empty bag for the no-capabilities default — every capability-backed tool
# degrades uniformly. Never mutated (only construction sites add), so safe to share.
NO_CAPS = ServiceContainer()


@dataclass
class TurnResult:
    """What one turn produced: a final answer (or None if it parked/blocked/hit
    a bound) and the message history needed to continue the conversation."""

    answer: str | None
    messages: list[ModelMessage] = field(default_factory=list)
    # A verifier correction's [reject_idx, nudge_idx] range to drop on persist.
    clean_drop: tuple[int, int] | None = None
    # Set when the turn stopped at a bound (`run.status is blocked`) — the
    # human-readable reason, carried through to `finalize` so it can persist a
    # marker on the turn's branch node (see `ConversationStore.record`).
    blocked_reason: str | None = None
    # Whether a spawned agent filed its report during the turn.
    reported: bool = False


def turn_deps(
    run: Run,
    *,
    caps: ServiceContainer = NO_CAPS,
    disabled_tools: frozenset[str] = NO_DISABLED_TOOLS,
    conversation_id: str | None = None,
    binding: ConversationBinding = DEFAULT_BINDING,
    workspace_key: str = "",
    turn_start: TurnStart | None = None,
    review_budget: ReviewBudget | None = None,
) -> RunDeps:
    """The ``RunDeps`` a turn runs its agent with.

    A function rather than a construction inside :func:`drive_turn` because a compaction
    summary is written on the turn's own agent (``agent/compaction_summary.py``), and the
    request it sends has to render the same brief and offer the same tools a turn would —
    the workspace, the level, the disabled set and the capability bag all shape one or the
    other. Two constructions of the same deps would be two answers to that question."""
    return RunDeps(
        run=run,
        owner_id=run.owner_id,
        # The whole agent-facing capability bag rides in as one handle — a tool
        # resolves what it needs by type and degrades when it's absent.
        caps=caps,
        disabled_tools=disabled_tools,
        conversation_id=conversation_id,
        # Where this run's file work happens. Resolved from the *conversation's* stored
        # binding by the caller, never from a live request — switching the active project
        # must not change what an already-running thread is doing.
        project_id=binding.project_id,
        worktree=binding.worktree,
        leases_from=binding.leases_from,
        checkout_read_only=binding.checkout_read_only,
        # And how far it may go on its own. The toolset stack reads this to mark the tools
        # that reach past it, so the level is enforced before a call runs rather than
        # apologised for afterwards.
        permission=binding.permission,
        # Which tools the thread's agent is offered at all, before anything else withholds.
        tool_access=binding.tools,
        permission_ceiling=binding.permission_ceiling,
        reports_to_parent=binding.spawned,
        # Which workspace this run's file work happens in. Empty — every turn an operator
        # sends — fills itself in from the conversation, which is the ordinary case and
        # needs no argument anywhere. A sub-agent's run is the case that passes one: it
        # works in the workspace of the thread that launched it, or in a delegated child of
        # it, and neither is named by its own conversation (`services/workspace.py`).
        workspace_key=workspace_key,
        # What a ruling made inside a tool call needs of the turn — the boundary the
        # review's transcript opens on, and the review budget (`agent/code_mode.py`).
        turn_start=turn_start,
        review_budget=review_budget,
    )


#: The wait before each resend of a request a transient model error cut short.
TRANSIENT_BACKOFF_S = (2.0, 8.0)


#: No request or tool-call budget: a turn is bounded by the loop guard and the timeouts.
#: Explicit because the library applies a 50-request cap when ``usage_limits`` is omitted.
UNBOUNDED = UsageLimits(request_limit=None)


def _resume_point(messages: list[ModelMessage]) -> list[ModelMessage] | None:
    """The history to resume a stopped turn from, ending in the request to send again.

    A stream cut mid-answer leaves its partial response at the tail; dropping it re-sends
    the request that produced it. None when there is no request to resume from.
    """
    history = messages[:-1] if messages and isinstance(messages[-1], ModelResponse) else messages
    if not history or not isinstance(history[-1], ModelRequest):
        return None
    return history


async def drive_turn(
    run: Run,
    agent: TurnAgent,
    *,
    settings: Settings,
    prompt: str | Sequence[UserContent] | None = None,
    message_history: list[ModelMessage] | None = None,
    deferred_results: DeferredToolResults | None = None,
    announced: set[str],
    caps: ServiceContainer = NO_CAPS,
    conversation_id: str | None = None,
    disabled_tools: frozenset[str] = NO_DISABLED_TOOLS,
    binding: ConversationBinding = DEFAULT_BINDING,
    vision: bool = True,
    partial_history_ref: list[Callable[[], list[ModelMessage]]] | None = None,
    store: ConversationStore | None = None,
    compaction: CompactionContext | None = None,
    turn_start: TurnStart | None = None,
    correcting: bool = False,
    workspace_key: str = "",
    reported: bool = False,
) -> TurnResult:
    """Drive one turn to its end: an answer, a park, or a stop at a bound.

    ``turn_start`` is the caller's persistence boundary — where in the replayed history
    *this turn's* own messages begin — held as one shared mutable object because an in-turn
    fold moves it. Every reader of that boundary (the completed persist, the flush hooks, a
    park) reads it through this object, so a fold cannot leave one of them slicing against
    the pre-fold history. The Auto review reads it too, for a different question with the
    same answer: which of these messages are the request this turn is being asked to
    authorize against (``gating.py``).

    ``compaction`` is what this turn may fold with, or ``None`` for a turn that cannot fold
    (a stateless run, or compaction switched off). ``correcting`` marks a verifier's
    corrective re-attempt, where a fold is refused: the correction's drop range is a pair
    of absolute indices into the pre-fold history, and folding underneath it would leave
    the persist dropping two messages that are no longer the ones it meant. ``reported``
    carries a spawned agent's filed report into that re-attempt, which is the same turn."""
    # ONE review budget for the whole turn: at the Auto level every hop's deferred calls
    # are reviewed, and a model that keeps reaching past the level's ceiling would
    # otherwise spend a utility-model round trip per call for as long as the turn runs.
    # Past the cap the calls park — the operator is asked the question the review was
    # answering for them, which is what every other degrade here does. It rides on the
    # deps as well, so a call a `run_code` script makes spends from the same counter.
    review_budget = ReviewBudget(limit=settings.review_max_per_turn)
    deps = turn_deps(
        run,
        caps=caps,
        disabled_tools=disabled_tools,
        conversation_id=conversation_id,
        binding=binding,
        workspace_key=workspace_key,
        turn_start=turn_start,
        review_budget=review_budget,
    )
    deps.reported = reported
    # A turn may run as several segments: the initial model pass, then a continuation
    # for each batch of deferred calls a conversation grant auto-approves. They share
    # ONE no-progress guard and ONE usage accumulator, so a granted tool the model keeps
    # re-calling can't reset the guard by deferring on each hop; it trips the loop stop.
    loop_breaker = LoopBreaker(repeat_threshold=settings.loop_repeat_threshold)
    usage = RunUsage()
    open_tasks_raised = False

    def report_progress(history: list[ModelMessage]) -> None:
        # A live context/usage frame as each model response lands, so the operator's context
        # gauge fills in real time during a live turn. Also the loop's cooperative-cancel
        # check: `RunRegistry.cancel()` sets `cancel_requested` and then hard-cancels the
        # task immediately, so in practice the native cancellation almost always lands first —
        # this is a redundant, independent stop path for the rare case something upstream
        # swallows that CancelledError (e.g. a tool/dependency catching too broadly), so a
        # requested cancel can never be silently absorbed.
        if run.cancel_requested:
            raise asyncio.CancelledError()
        _ = run.emit(turn_metrics(run, history, settings=settings))

    # Rebound each loop iteration by `agent.iter()`'s `as agent_run`; stays None only
    # if a bound trips before the context manager assigns it (its `__aenter__` does
    # no request, so this hasn't been observed, but the except blocks below guard
    # it anyway rather than risk an unbound-variable crash on a stop path).
    agent_run: AgentRun[RunDeps, DeferredToolRequests | str] | None = None

    def _partial_history() -> list[ModelMessage]:
        return list(agent_run.ctx.state.message_history) if agent_run is not None else []

    def _inject_queued(node: ModelRequestNode[RunDeps, object]) -> None:
        # Mid-run steering: hand any operator messages queued since the last model
        # request to the model *now*, by amending the not-yet-sent request. The node
        # is yielded before it streams (its request isn't in history yet), so this
        # never touches an in-flight model stream; a request mixing tool returns
        # with these user parts is split back into separate messages on persist
        # (`split_injected_requests`), which replays wire-identically because the
        # library re-merges consecutive requests at wire-prep.
        #
        # Rebinds `parts` to a NEW list rather than appending in place. On a regenerate
        # the node's request is built by the library reusing the *same* parts list object
        # as the last history message — which the store handed out by reference from its
        # in-memory tree. Appending would therefore graft the steering text into the
        # operator's original user bubble for every later replay. Same invariant
        # `with_tail_context` documents: never mutate what the store shares.
        queued = run.drain_messages()
        if queued:
            node.request.parts = [
                *node.request.parts,
                *(UserPromptPart(injected_text(message)) for message in queued),
            ]

    if partial_history_ref is not None:
        # Let the caller reach this turn's current partial state at any point — even
        # before the first step lands — so an external timeout mid-turn can still flush
        # whatever's there (see `build_chat_orchestrator`'s `on_timeout` hook).
        partial_history_ref[:] = [_partial_history]

    # One recovery per turn. A second overflow after a fold means the request is oversized
    # for a reason folding cannot reach (one enormous tool result, an attachment that
    # doesn't fit), and re-folding would strip the thread's memory for nothing.
    compacted = False
    transient_retries = 0

    while True:
        # Same cooperative-cancel check as `report_progress`, at the turn-segment
        # boundary between an auto-approved tool's continuation and the next model
        # round-trip (see that function's comment for why this is a redundant,
        # not primary, stop path).
        if run.cancel_requested:
            raise asyncio.CancelledError()
        try:
            async with agent.iter(
                prompt,
                deps=deps,
                message_history=message_history,
                deferred_tool_results=deferred_results,
                usage_limits=UNBOUNDED,
                usage=usage,
            ) as agent_run:
                await stream_agent_run(
                    agent_run,
                    run,
                    announced=announced,
                    loop_breaker=loop_breaker,
                    on_step=report_progress,
                    on_request_node=_inject_queued,
                    first_token_timeout_s=settings.first_token_timeout_s,
                )
                result = agent_run.result
                assert result is not None
        except FirstTokenTimeout as exc:
            detail = f"stopped: {exc}"
            _ = run.emit(LimitNotice(limit="time", message=detail))
            run.block(detail)
            return TurnResult(answer=None, messages=_partial_history(), blocked_reason=detail)
        except LoopDetected as exc:
            # No-progress guard tripped — stop and report state, don't error.
            _ = run.emit(LimitNotice(limit="loop", message=str(exc)))
            detail = "stopped: repeated an action without making progress"
            run.block(detail)
            return TurnResult(
                answer=None,
                messages=_partial_history(),
                blocked_reason=detail,
            )
        except (ModelAPIError, httpx.TransportError) as exc:
            # Context-window overflow. The request that overran is still the tail of the
            # partial history and would replay on every later turn, so this cannot simply
            # be reported: fold the thread once and re-send the same request against the
            # summary. If there is nothing to fold, folding is refused here, or the retry
            # overruns again, it is a definitive ceiling — stop and name it, rather than
            # papering over it by silently dropping content.
            if isinstance(exc, ModelHTTPError) and is_context_overflow(exc):
                rebuilt = (
                    None
                    if compacted or correcting or compaction is None or turn_start is None
                    else await compact_and_retry(
                        run,
                        # The summary is written on *this* segment's agent and deps: on a
                        # resume the context came off the park, whose deps hold the run
                        # that parked rather than the one now driving the turn.
                        replace(compaction, agent=agent, deps=deps),
                        partial_history=_partial_history(),
                        turn_start=turn_start,
                    )
                )
                if rebuilt is not None:
                    compacted = True
                    # The resume-without-prompt shape: the library pops the trailing
                    # request and re-sends its parts, so the very request that overran is
                    # the one retried — against the folded history in front of it.
                    prompt = None
                    message_history = rebuilt
                    deferred_results = None
                    continue
                # Which of the two overflow stops this is travels as the *detail*, not
                # only as prose in the notice: the client offers "Compact and retry" on
                # this marker, and a turn that already folded and still overran is the one
                # case where that button would send the operator round the same loop.
                detail = (
                    CONTEXT_OVERFLOW_AFTER_FOLD_DETAIL if compacted else CONTEXT_OVERFLOW_DETAIL
                )
                _ = run.emit(
                    LimitNotice(
                        limit="context",
                        message=context_limit_message(run, compacted=compacted),
                        detail=detail,
                    )
                )
                run.block(detail)
                return TurnResult(
                    answer=None,
                    messages=_partial_history(),
                    blocked_reason=detail,
                )
            # Rewrite a model-couldn't-load error into something the operator can act on.
            hint = model_load_hint(exc) if isinstance(exc, ModelHTTPError) else None
            if hint is not None:
                raise ModelLoadError(hint) from exc
            # A dropped connection, an overloaded server or a stream cut short is worth
            # sending again from the last complete request, a couple of times, before
            # the error propagates with its own detail.
            resume = _resume_point(_partial_history()) if is_transient(exc) else None
            if resume is None or transient_retries >= settings.auto_continue_transient_max:
                raise
            transient_retries += 1
            run.preparing_calls.clear()
            if run.step_text_chars or run.step_thinking_chars:
                _ = run.emit(
                    AnswerRetracted(
                        text_chars=run.step_text_chars, thinking_chars=run.step_thinking_chars
                    )
                )
                run.step_text_chars = run.step_thinking_chars = 0
            _ = run.emit(
                LimitNotice(
                    limit="continue",
                    message=(
                        f"The model connection failed ({type(exc).__name__}); retrying "
                        + f"({transient_retries} of {settings.auto_continue_transient_max})"
                    ),
                )
            )
            await asyncio.sleep(
                TRANSIENT_BACKOFF_S[min(transient_retries, len(TRANSIENT_BACKOFF_S)) - 1]
            )
            prompt = None
            message_history = resume
            deferred_results = None
            continue

        output = result.output
        messages = result.all_messages()
        # Counted off the full replayed history, so hops and segments need no
        # accumulator — see `turn_metrics`.
        run.set_metrics(turn_metrics(run, messages, settings=settings))
        # Two ways a turn can end without being finished: a call awaiting permission, and
        # a call awaiting an answer (`ask_user`). Both arrive on the same object, and
        # either one alone means there is more of this turn to run.
        if not (isinstance(output, DeferredToolRequests) and (output.approvals or output.calls)):
            # About to stop with nothing else to take: an open task list gets one word first.
            # Not after a spawned agent's report, which told it to end without another word.
            if (
                not open_tasks_raised
                and conversation_id is not None
                and not deps.reported
                and not run.messages.has_deliverable()
            ):
                notice = await open_tasks_notice(caps, run.owner_id, conversation_id)
                if notice is not None:
                    open_tasks_raised = True
                    _ = run.messages.enqueue(notice, source="system", notice="open-tasks")
            # The model finished, but something queued while it was working: instead of
            # ending the run, continue it with the queued text as the next user
            # request(s) — same run id, same stream, same loop guard and timeouts.
            # `prompt=None` + a history ending in a user request is the same continuation
            # shape a regenerate uses.
            #
            # Through `injected_text` like the boundary above, and for the reason there is
            # only one of it: this is where most sub-agent reports actually land — one
            # that finishes while the parent is mid-answer arrives *here*, not at a
            # mid-stream boundary — and a report handed over bare is the operator credited
            # with words they have not read, in the history every later turn replays.
            pending = run.drain_messages()
            if pending:
                message_history = messages + [
                    ModelRequest(parts=[UserPromptPart(injected_text(m))]) for m in pending
                ]
                prompt = None
                deferred_results = None
                continue
            answer = output if isinstance(output, str) else None
            return TurnResult(answer=answer, messages=messages, reported=deps.reported)

        # Rule on each deferred *approval* — grants, then the level, then Auto's review.
        # The rules and their announcement live in `gating.py`; what is left here is what
        # the turn does with the two piles it hands back. Questions (`output.calls`) are
        # not put through any of it: a grant, a level and a reviewer all answer "may this
        # run", and a question is not asking that.
        settled, manual = await settle_deferred(
            run,
            output.approvals,
            caps=caps,
            conversation_id=conversation_id,
            deps=deps,
            messages=messages,
            permission=binding.permission,
            # Where this turn's own messages begin, so the review is given the request
            # that opened it however many tool round trips have run since.
            turn_start=turn_start,
            budget=review_budget,
        )
        if manual or output.calls:
            await park_for_input(
                run,
                agent,
                messages,
                output,
                announced,
                settled=settled,
                notifications=caps.get_optional(NotificationService),
                store=store,
                conversation_id=conversation_id,
                binding=binding,
                vision=vision,
                # Carried so a resume can recover from an overflow the way this turn would
                # have: the operator may take hours to answer, and the thread they come
                # back to is the one that was already near its ceiling.
                compaction=compaction,
                # And the workspace it parked in — `deps.workspace_key` rather than the
                # argument, so a turn that filled the default in from its conversation
                # carries the resolved key rather than an empty one.
                workspace_key=deps.workspace_key,
            )
            return TurnResult(answer=None, messages=messages)
        # Every deferred call settled without the operator — no question was asked, and
        # every approval was ruled on. Continue the SAME turn inline
        # (no round-trip), reusing the shared budget/guard/usage above. An auto-run tool
        # still streams its tool.started/completed, so it stays visible, and a call the
        # level refused comes back to the model as a denial it can plan around. Defensively
        # resolve any approval_needed notification still pending for this run — normally
        # a no-op (this branch only runs when nothing this hop parked), but idempotent
        # against whatever multi-hop history led here, so nothing is ever left dangling.
        notifications = caps.get_optional(NotificationService)
        if notifications is not None:
            with suppress(Exception):
                _ = await notifications.resolve_for_run(run.owner_id, run.id)
        prompt = None
        message_history = messages
        approvals: dict[str, DeferredToolApprovalResult | bool] = {**settled}
        deferred_results = DeferredToolResults(approvals=approvals)
