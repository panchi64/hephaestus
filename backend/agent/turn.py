"""One turn, driven to its end: an answer, a park, or a stop at a bound.

The loop around ``agent.iter()``: the initial model pass, then a continuation for each
batch of deferred calls that settles without the operator, sharing one no-progress guard,
usage accumulator and review budget. It emits the live gauge frame per response, drains
mid-run steering at both boundaries, and handles the stops that are state rather than
errors (first-token timeout, loop breaker, context overflow, a call needing the operator).

Both orchestrators and the verifier's corrective re-attempt run it. Policy lives
elsewhere (``gating.py``, ``parking.py``, ``folding.py``, ``model_errors.py``); this is
control flow. It reads no settings itself: the caller passes one object so every bound and
frame measures against the same values.
"""

from __future__ import annotations

import asyncio
import math
import random
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

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
from pydantic_ai.exceptions import ModelAPIError, ModelHTTPError, UsageLimitExceeded
from pydantic_ai.messages import UserContent
from pydantic_ai.tools import DeferredToolApprovalResult

from core.config import Settings
from core.container import ServiceContainer
from core.exceptions import ModelLoadError
from runs import AnswerRetracted, LimitNotice, Run
from runs.run import StopCause
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

# Shared empty bag for the no-capabilities default; never mutated, so safe to share.
NO_CAPS = ServiceContainer()


@dataclass
class TurnResult:
    """What one turn produced: a final answer (None if it parked, blocked or hit a bound)
    and the history to continue from."""

    answer: str | None
    messages: list[ModelMessage] = field(default_factory=list)
    # A verifier correction's [reject_idx, nudge_idx] range to drop on persist.
    clean_drop: tuple[int, int] | None = None
    # The reason a bound stopped the turn (`run.status is blocked`), persisted by `finalize`
    # as a marker on the branch node (`ConversationStore.record`).
    blocked_reason: str | None = None
    #: The binding an answered turn ended under, carrying any level `plan_enter` or an
    #: approved plan moved it to, so a verifier correction continues at that level.
    binding: ConversationBinding | None = None
    #: Whether a spawned agent filed its report during the turn.
    reported: bool = False
    #: Model requests the turn has spent, counted across its segments.
    requests_used: int = 0


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
    max_requests: int | None = None,
    ledger_prompt_message_id: str | None = None,
) -> RunDeps:
    """The ``RunDeps`` a turn runs its agent with.

    A function because a compaction summary is written on the turn's own agent
    (``agent/compaction_summary.py``) and must render the same brief and offer the same
    tools; the workspace, level, disabled set and capability bag shape both.
    """
    return RunDeps(
        run=run,
        owner_id=run.owner_id,
        # The whole capability bag as one handle; a tool resolves what it needs by type.
        caps=caps,
        disabled_tools=disabled_tools,
        conversation_id=conversation_id,
        # From the conversation's stored binding, never a live request: switching the
        # active project must not change a running thread.
        project_id=binding.project_id,
        worktree=binding.worktree,
        leases_from=binding.leases_from,
        checkout_read_only=binding.checkout_read_only,
        # The toolset stack marks tools reaching past it, enforcing the level before a call runs.
        permission=binding.permission,
        # Which tools the thread's agent is offered at all, before anything else withholds.
        tool_access=binding.tools,
        permission_ceiling=binding.permission_ceiling,
        reports_to_parent=binding.spawned,
        # Empty (an operator's turn) fills in from the conversation. A sub-agent passes the
        # launching thread's workspace or a delegated child's (`services/workspace.py`).
        workspace_key=workspace_key,
        # What a ruling inside a tool call needs: the review transcript's boundary and the
        # review budget (`agent/code_mode.py`).
        turn_start=turn_start,
        review_budget=review_budget,
        max_requests=max_requests,
        ledger_prompt_message_id=ledger_prompt_message_id,
    )


#: The wait before each resend of a request a transient model error cut short.
TRANSIENT_BACKOFF_S = (2.0, 8.0)
_JITTER = 0.2
#: The longest a provider's Retry-After is honoured for.
RETRY_AFTER_CAP_S = 60.0

TURN_LIMIT_DETAIL = "stopped: reached the agent's request limit"


def _retry_after_s(exc: Exception) -> float | None:
    """The provider's Retry-After in seconds (or HTTP date), or None if absent or unreadable."""
    headers = exc.headers if isinstance(exc, ModelHTTPError) else None
    raw = headers.get("retry-after") if headers else None
    if raw is None:
        return None
    raw = raw.strip()
    try:
        seconds = float(raw)
    except ValueError:
        pass
    else:
        return None if math.isnan(seconds) else max(seconds, 0.0)
    try:
        when = parsedate_to_datetime(raw)
    except TypeError, ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max((when - datetime.now(UTC)).total_seconds(), 0.0)


def transient_wait_s(exc: Exception, retries: int) -> float:
    """Wait before resend number ``retries`` (1-based): Retry-After floored at the backoff
    step and capped, else the step with ±20% jitter."""
    step = TRANSIENT_BACKOFF_S[min(retries, len(TRANSIENT_BACKOFF_S)) - 1]
    step *= random.uniform(1 - _JITTER, 1 + _JITTER)
    advised = _retry_after_s(exc)
    if advised is None:
        return step
    return min(max(advised, step), RETRY_AFTER_CAP_S)


def _request_limits(max_requests: int | None) -> UsageLimits:
    """No request budget unless the agent set one; explicit because the library caps at 50
    requests when ``usage_limits`` is omitted."""
    return UsageLimits(request_limit=max_requests)


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
    max_requests: int | None = None,
    requests_used: int = 0,
    ledger_prompt_message_id: str | None = None,
) -> TurnResult:
    """Drive one turn to its end: an answer, a park, or a stop at a bound.

    ``turn_start`` is the persistence boundary, where this turn's own messages begin in the
    replayed history, held as one shared mutable object because an in-turn fold moves it;
    the persist, flush hooks, park and Auto review (``gating.py``) all read through it.

    ``compaction`` is what this turn may fold with, ``None`` if it cannot (stateless, or
    compaction off). ``correcting`` marks a verifier re-attempt, where folding is refused:
    its drop range holds absolute pre-fold indices. ``reported`` carries a spawned agent's
    filed report into that re-attempt, which is the same turn."""
    # One review budget for the whole turn: at Auto every hop's deferred calls are
    # reviewed, and a model reaching past the ceiling would otherwise spend a utility-model
    # call each, indefinitely. Past the cap calls park for the operator. It rides on the
    # deps so `run_code` calls spend from the same counter.
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
        max_requests=max_requests,
        ledger_prompt_message_id=ledger_prompt_message_id,
    )
    deps.reported = reported
    usage_limits = _request_limits(max_requests)
    # Segments share one no-progress guard and usage accumulator, so a granted tool the
    # model keeps re-calling can't reset the guard by deferring on each hop.
    loop_breaker = LoopBreaker(repeat_threshold=settings.loop_repeat_threshold)
    # Seeded with earlier segments' requests so the cap bounds the whole turn, queued-message
    # continuations included.
    usage = RunUsage(requests=requests_used)
    open_tasks_raised = False

    def report_progress(history: list[ModelMessage]) -> None:
        # A live gauge frame as each response lands. Also a cooperative-cancel check,
        # redundant with the task cancel `RunRegistry.cancel()` issues, for when something
        # upstream swallows that CancelledError (a tool catching too broadly).
        if run.cancel_requested:
            raise asyncio.CancelledError()
        _ = run.emit(turn_metrics(run, history, settings=settings))

    # Rebound each iteration by `agent.iter()`; None only if a bound trips before the
    # context manager assigns it, which the except blocks guard against.
    agent_run: AgentRun[RunDeps, DeferredToolRequests | str] | None = None

    def _partial_history() -> list[ModelMessage]:
        return list(agent_run.ctx.state.message_history) if agent_run is not None else []

    def _inject_queued(node: ModelRequestNode[RunDeps, object]) -> None:
        # Mid-run steering: amend the not-yet-sent request with operator messages queued
        # since the last one. The node is yielded before streaming, so no in-flight stream
        # is touched; a request mixing tool returns with these user parts is split on persist
        # (`split_injected_requests`) and replays wire-identically, as the library re-merges
        # consecutive requests.
        #
        # Rebinds `parts` to a new list: on a regenerate the request reuses the parts list
        # the store shares by reference, so appending would graft the steering text into the
        # operator's original bubble. Same invariant as `with_tail_context`.
        queued = run.drain_messages()
        if queued:
            node.request.parts = [
                *node.request.parts,
                *(UserPromptPart(injected_text(message)) for message in queued),
            ]

    if partial_history_ref is not None:
        # Lets an external timeout flush the partial state at any point, even before the
        # first step lands (`build_chat_orchestrator`'s `on_timeout` hook).
        partial_history_ref[:] = [_partial_history]

    # One recovery per turn: a second overflow after a fold means something folding can't
    # reach (one enormous tool result, an attachment), and re-folding would strip the
    # thread's memory for nothing.
    compacted = False
    transient_retries = 0

    while True:
        # Redundant cancel check at the segment boundary (see `report_progress`).
        if run.cancel_requested:
            raise asyncio.CancelledError()
        try:
            async with agent.iter(
                prompt,
                deps=deps,
                message_history=message_history,
                deferred_tool_results=deferred_results,
                usage_limits=usage_limits,
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
            run.end_code = "first_token"
            return TurnResult(answer=None, messages=_partial_history(), blocked_reason=detail)
        except UsageLimitExceeded:
            _ = run.emit(LimitNotice(limit="turn_limit", message=TURN_LIMIT_DETAIL))
            run.block(TURN_LIMIT_DETAIL)
            run.end_code = "turn_limit"
            run.stop_cause = StopCause.turn_limit
            return TurnResult(
                answer=None, messages=_partial_history(), blocked_reason=TURN_LIMIT_DETAIL
            )
        except LoopDetected as exc:
            _ = run.emit(LimitNotice(limit="loop", message=str(exc)))
            detail = "stopped: repeated an action without making progress"
            run.block(detail)
            run.end_code = "loop"
            return TurnResult(
                answer=None,
                messages=_partial_history(),
                blocked_reason=detail,
            )
        except (ModelAPIError, httpx.TransportError) as exc:
            # Context overflow: the overrunning request is still the history's tail and would
            # replay every later turn, so fold once and re-send it against the summary. If
            # nothing folds, folding is refused, or the retry overruns, it is a definitive
            # ceiling: stop and name it rather than silently dropping content.
            if isinstance(exc, ModelHTTPError) and is_context_overflow(exc):
                rebuilt = (
                    None
                    if compacted or correcting or compaction is None or turn_start is None
                    else await compact_and_retry(
                        run,
                        # This segment's agent and deps: on a resume the context's deps hold
                        # the run that parked, not the one now driving.
                        replace(compaction, agent=agent, deps=deps),
                        partial_history=_partial_history(),
                        turn_start=turn_start,
                    )
                )
                if rebuilt is not None:
                    compacted = True
                    # The library pops the trailing request and re-sends its parts, so the
                    # overrunning request is retried against the folded history.
                    prompt = None
                    message_history = rebuilt
                    deferred_results = None
                    continue
                # Carried as the detail, not just prose: the client offers "Compact and
                # retry" on it, which would loop if the turn already folded.
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
                run.end_code = "context_overflow"
                return TurnResult(
                    answer=None,
                    messages=_partial_history(),
                    blocked_reason=detail,
                )
            hint = model_load_hint(exc) if isinstance(exc, ModelHTTPError) else None
            if hint is not None:
                raise ModelLoadError(hint) from exc
            # Transient failures are re-sent from the last complete request a few times first.
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
            wait_s = transient_wait_s(exc, transient_retries)
            _ = run.emit(
                LimitNotice(
                    limit="continue",
                    message=(
                        f"The model connection failed ({type(exc).__name__}); retrying in "
                        + f"{wait_s:.0f}s ({transient_retries} of "
                        + f"{settings.auto_continue_transient_max})"
                    ),
                )
            )
            # A wait can outlast the inactivity bound, and nothing else is emitting.
            async with run.keepalive():
                await asyncio.sleep(wait_s)
            prompt = None
            message_history = resume
            deferred_results = None
            continue

        output = result.output
        messages = result.all_messages()
        # Counted off the full replayed history, so segments need no accumulator.
        run.set_metrics(turn_metrics(run, messages, settings=settings))
        # A turn is unfinished while a call awaits permission or an answer (`ask_user`);
        # both arrive on the same object.
        if not (isinstance(output, DeferredToolRequests) and (output.approvals or output.calls)):
            # An open task list gets one word before stopping, but not after a spawned
            # agent's report, which told it to end without another.
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
            # Something queued while the model worked: continue the same run (stream, loop
            # guard, timeouts) with it as the next user request(s); `prompt=None` plus a
            # history ending in a user request is the regenerate shape.
            #
            # Through `injected_text` like the boundary above: most sub-agent reports land
            # here, and a bare report would credit the operator with words they never read
            # in the history every later turn replays.
            pending = run.drain_messages()
            if pending:
                message_history = messages + [
                    ModelRequest(parts=[UserPromptPart(injected_text(m))]) for m in pending
                ]
                prompt = None
                deferred_results = None
                continue
            answer = output if isinstance(output, str) else None
            return TurnResult(
                answer=answer,
                messages=messages,
                binding=replace(binding, permission=deps.permission),
                reported=deps.reported,
                requests_used=usage.requests,
            )

        # Rule on each deferred approval (grants, level, Auto's review) in `gating.py`.
        # Questions (`output.calls`) skip it: those rulings answer "may this run".
        settled, manual = await settle_deferred(
            run,
            output.approvals,
            caps=caps,
            conversation_id=conversation_id,
            deps=deps,
            messages=messages,
            # The live level: `plan_enter` and an approved plan move it mid-turn
            # (`tools/plan.retarget`).
            permission=deps.permission,
            # So the review sees the request that opened the turn however many round trips ran.
            turn_start=turn_start,
            budget=review_budget,
            metadata=output.metadata,
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
                # Live level, so a resume after a revision request stays in plan mode.
                binding=replace(binding, permission=deps.permission),
                vision=vision,
                # So a resume can recover from an overflow: the thread may be near its ceiling.
                compaction=compaction,
                # `deps.workspace_key`, not the argument, which may still be the empty default.
                workspace_key=deps.workspace_key,
                max_requests=max_requests,
                requests_used=usage.requests,
                ledger_prompt_message_id=ledger_prompt_message_id,
            )
            return TurnResult(answer=None, messages=messages)
        # Everything settled without the operator: continue the same turn inline, sharing
        # the budget, guard and usage. Refused calls return to the model as denials. The
        # notification resolve is normally a no-op but idempotent, so no multi-hop history
        # leaves an approval_needed dangling.
        notifications = caps.get_optional(NotificationService)
        if notifications is not None:
            with suppress(Exception):
                _ = await notifications.resolve_for_run(run.owner_id, run.id)
        prompt = None
        message_history = messages
        approvals: dict[str, DeferredToolApprovalResult | bool] = {**settled}
        deferred_results = DeferredToolResults(approvals=approvals)
