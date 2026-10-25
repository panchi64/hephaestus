"""Substrate HTTP surface: observe, stream, and cancel a Run.

Creating a Run is a feature concern (the chat and agent routes call
``deps.registry(request).submit(...)``), not here.
"""

from __future__ import annotations

import contextlib
from collections.abc import Sequence
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field
from pydantic_ai import ToolApproved, ToolDenied
from pydantic_ai.messages import ToolCallPart

from agent import ParkedTurn, build_resume_orchestrator
from agent.approval_guards import guarded_approvals
from agent.gating import GrantApproved
from agent.recompose import RecomposeError, recompose
from core.auth import via_api_token
from core.config import get_settings
from routes import deps
from runs import (
    AnsweredQuestionOut,
    QuestionAnswered,
    Run,
    RunStatus,
    parse_last_event_id,
    sse_response,
)
from runs.run import StopCause
from services.answers import (
    AnswerError,
    answered_questions,
    questions_of,
    render_answer,
)
from services.approval_grants import GrantInfo, grant_scopes, grant_width, grantable
from services.approval_ledger import LedgerEntry
from services.plan_mode import PLAN_SUBMIT_TOOL
from services.settings_store import get_inactivity_timeout, get_wall_clock_timeout
from services.subagents import SubtreeStopper

router = APIRouter(prefix="/runs", tags=["runs"])


def refusal_message(intent: str, written: str | None) -> str:
    """What the model is told in place of the result, when the operator said no.

    Denial and revision are different instructions: a denial read as "try again" repeats a
    refused act, and a revision read as a flat no ends in an apology. The operator's words
    are quoted; the intent decides the framing.
    """
    if intent == "revise":
        detail = f": {written}" if written else ", without saying what to change"
        return (
            f"The operator did not approve this and asked for a different version of it"
            f"{detail}. Revise it and propose again — do not carry on as though it had "
            "been approved, and do not abandon the work."
        )
    detail = f" {written}" if written else ""
    return f"The operator denied this action.{detail}"


class RunView(BaseModel):
    # The older fields are snake_case; the two conversation fields are camelCase to match
    # the newer surfaces (corpus, uploads, notifications).
    id: str
    kind: str
    status: str
    owner_id: str
    detail: str | None = None
    error: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    ended_at: datetime | None = None
    last_seq: int
    conversation_id: str | None = Field(default=None, serialization_alias="conversationId")
    conversation_title: str | None = Field(default=None, serialization_alias="conversationTitle")


def _view(run: Run, title: str | None) -> RunView:
    """One run for the client; the title is passed in so a listing resolves all in one query."""
    return RunView(
        id=run.id,
        kind=run.kind,
        status=run.status.value,
        owner_id=run.owner_id,
        detail=run.detail,
        error=run.error,
        created_at=run.created_at,
        started_at=run.started_at,
        ended_at=run.ended_at,
        last_seq=run.stream.last_seq,
        conversation_id=run.conversation_id,
        conversation_title=title,
    )


def _require_run(request: Request, run_id: str) -> Run:
    run = deps.registry(request).get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    return run


@router.get("", response_model=list[RunView])
async def list_runs(request: Request, active: Annotated[bool, Query()] = True) -> list[RunView]:
    """The operator's runs, newest first; ``active=True`` (default) keeps only non-terminal ones."""
    runs = deps.registry(request).list(deps.OPERATOR_ID)
    if active:
        runs = [r for r in runs if not r.is_terminal]
    runs.sort(key=lambda r: r.created_at, reverse=True)
    titles = await deps.store(request).titles(
        [r.conversation_id for r in runs if r.conversation_id is not None],
        deps.OPERATOR_ID,
    )
    return [_view(r, titles.get(r.conversation_id) if r.conversation_id else None) for r in runs]


@router.get("/{run_id}", response_model=RunView)
async def get_run(run_id: str, request: Request) -> RunView:
    run = _require_run(request, run_id)
    titles = (
        await deps.store(request).titles([run.conversation_id], run.owner_id)
        if run.conversation_id is not None
        else {}
    )
    return _view(run, titles.get(run.conversation_id or ""))


@router.get("/{run_id}/events")
async def stream_run_events(
    run_id: str,
    request: Request,
    last_event_id: Annotated[int | None, Query()] = None,
):
    """SSE event stream. Reconnect with ``Last-Event-ID`` to replay missed events. A client
    on an API token tails the run without counting as the operator watching it."""
    run = _require_run(request, run_id)
    after = parse_last_event_id(request.headers.get("last-event-id"), last_event_id)
    return sse_response(run, after, watching=not via_api_token(request.state))


class CancelRequest(BaseModel):
    #: The operator's Stop on their own thread: everything waiting in its inbox stays there,
    #: behind their next message, instead of starting the next turn.
    hold_inbox: bool = False


@router.post("/{run_id}/cancel", status_code=202)
async def cancel_run(
    run_id: str, request: Request, body: CancelRequest | None = None
) -> dict[str, str]:
    registry = deps.registry(request)
    run = registry.get(run_id)
    held = None
    if body is not None and body.hold_inbox and run is not None and run.conversation_id:
        # Before the cancel, so the run ending finds the inbox already held.
        inbox = registry.inbox(run.conversation_id, run.owner_id)
        held = (inbox, inbox.stop())
    if run is not None and not run.is_terminal and run.stop_cause is None:
        run.stop_cause = StopCause.operator
    cancelled = await registry.cancel(run_id)
    if not cancelled:
        if held is not None:
            held[0].undo_stop(held[1])
        run = registry.get(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        raise HTTPException(status_code=409, detail=f"run already {run.status.value}")
    stopper = deps.capabilities(request).get_optional(SubtreeStopper)
    if run is not None and run.conversation_id and stopper is not None:
        # The stopped agents' reports must not start a turn on the thread just stopped.
        inbox = registry.inbox(run.conversation_id, run.owner_id)
        mark = None if held is not None else inbox.stop()
        try:
            stopped = await stopper.stop_below(run.owner_id, run.conversation_id)
        except BaseException:
            if mark is not None:
                inbox.undo_stop(mark)
            raise
        if mark is not None and not stopped:
            inbox.undo_stop(mark)
    return {"status": "cancelling"}


class ApprovalDecision(BaseModel):
    tool_call_id: str
    approved: bool
    message: str | None = None  # shown to the model on denial
    override_args: dict[str, object] | None = None  # replace args on approval
    # "once" is this call only. "conversation" records a grant so the same act isn't
    # re-prompted for the thread (for a command tool, that command, derived from the parked
    # call). "conversation_tool" is the wider pick, the whole tool, offered separately
    # because it says far more and the narrow one must stay the easy path.
    scope: Literal["once", "conversation", "conversation_tool"] = "once"
    # Read only when `approved` is False; both stop the call and differ only in what the
    # model is told. "revise" asks for a different version, which models often mishandle
    # as a bare refusal. Generic: any deferred call can be sent back, a submitted plan is
    # just the first surface to offer it.
    intent: Literal["deny", "revise"] = "deny"


class QuestionReply(BaseModel):
    """What the operator said to one question."""

    # Labels as offered, not indices, so a reply can't land on the wrong option if the sides
    # disagree about order; the server checks them against the parked call regardless.
    selections: list[str] = Field(default_factory=list)
    # Always available: the options are the model's guesses, not the range of answers.
    text: str | None = None


class QuestionAnswer(BaseModel):
    """One parked ``ask_user`` call, answered; ``replies`` is positional, one per question."""

    tool_call_id: str
    replies: list[QuestionReply]


class ApprovalDecisions(BaseModel):
    decisions: list[ApprovalDecision] = Field(default_factory=list)
    answers: list[QuestionAnswer] = Field(default_factory=list)


class ApprovalOutcome(BaseModel):
    """What settling the park did: the resume, and what became of the standing yeses.

    A "for this conversation" opt-in on a command no scope could stand for records nothing
    (`services/approval_grants.py`), so the response says what was written and which calls
    could not be, rather than leaving a ticked box with no chip and no explanation.
    """

    status: Literal["resuming"] = "resuming"
    #: The scopes recorded, as their words (`[]` for a whole-tool grant).
    granted: list[list[str]] = Field(default_factory=list)
    #: Tool call ids whose conversation opt-in could not be scoped; the approval itself stands.
    unscoped: list[str] = Field(default_factory=list)


def _still_covered(call: ToolCallPart, settled: GrantApproved, active: Sequence[GrantInfo]) -> bool:
    """Whether the grant that settled this call still covers it, at the width it needed.

    A grant can be revoked or lapse while the run waits, so the allow is re-checked before
    resume. Only a whole-tool grant clears an act nobody can undo
    (:func:`services.permissions.decide.review`), so that allow is re-checked at the wider
    width: otherwise a surviving command-scoped grant would stand in for the revoked
    whole-tool one, which the risk axis refused. Other grant-driven allows need a grant of
    either width.
    """
    width = grant_width(call.tool_name, call.args_as_dict(), active)
    return width == "tool" if settled.needs_whole_tool else width != "none"


@router.post("/{run_id}/approve", status_code=202)
async def approve_run(run_id: str, body: ApprovalDecisions, request: Request) -> ApprovalOutcome:
    """Settle everything a parked run awaits, permissions and answers, then resume it.

    One endpoint because a run parks and resumes once: a turn stopped on an approval and a
    question waits on a single set.
    """
    registry = deps.registry(request)
    run = registry.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    if run.status is not RunStatus.awaiting_input or not isinstance(run.parked_payload, ParkedTurn):
        raise HTTPException(status_code=409, detail=f"run is not awaiting approval ({run.status})")

    parked: ParkedTurn = run.parked_payload
    # The operator decides only calls not already settled by an active grant or by the
    # permission level refusing outright; those ride on the parked payload and merge back below.
    call_by_id = {call.tool_call_id: call for call in parked.requests.approvals}
    pending = set(call_by_id) - set(parked.settled)
    # Questions are always pending (nothing settles one ahead of the operator), and the run
    # resumes once, so a body missing an answer would resume a call with nothing to hand it.
    asked = {call.tool_call_id: call.args_as_dict() for call in parked.requests.calls}
    # Checked per pile, not against the union: a swapped id would pass a union check and
    # then index the wrong map below, giving a 500 instead of a 400.
    decided = {d.tool_call_id for d in body.decisions}
    answered = {a.tool_call_id for a in body.answers}
    if decided != pending or answered != set(asked):
        raise HTTPException(
            status_code=400,
            detail=(
                "decisions must cover exactly the pending approvals "
                f"({sorted(pending)}) and answers exactly the pending questions "
                f"({sorted(asked)})"
            ),
        )

    # A guarded approval is the operator's alone, refused before anything below changes.
    if parked.agent is None and any(d.override_args is not None for d in body.decisions):
        # A restored park has no deps yet and the guard needs their home, so a token's
        # request may recompose before the 403 (idempotent). Failure is reported below.
        with contextlib.suppress(RecomposeError):
            _ = await recompose(registry, run, parked)
    guarded = await guarded_approvals(body.decisions, parked, deps.app_state(request).settings)
    if guarded:
        deps.require_operator(request)

    # Rendered from the parked arguments, not client prose: the client names the options
    # chosen and their text is read off the model's actual call, so transcript and model
    # can't see different questions.
    answers: dict[str, str] = {}
    # The same replies as structure for the client's card, built from the same inputs.
    announced: list[QuestionAnswered] = []
    for answer in body.answers:
        replies = [(r.selections, r.text) for r in answer.replies]
        questions = questions_of(asked[answer.tool_call_id])
        try:
            answers[answer.tool_call_id] = render_answer(questions, replies)
        except AnswerError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        announced.append(
            QuestionAnswered(
                tool_call_id=answer.tool_call_id,
                answers=[
                    AnsweredQuestionOut(
                        question=one.question, selections=one.selections, text=one.text
                    )
                    for one in answered_questions(questions, replies)
                ],
            )
        )
    # Emitted only after every answer renders: a refused second answer settles nothing, so
    # an earlier frame would announce an answer the run never got.
    for frame in announced:
        _ = run.emit(frame)

    grants = deps.approval_grants(request)
    # Re-validate grant-driven pre-approvals against current grants: a grant revoked or
    # lapsed while parked must not still auto-run its call.
    grant_approved = {
        call_id for call_id, o in parked.settled.items() if isinstance(o, GrantApproved)
    }
    active = (
        await grants.list(deps.OPERATOR_ID, parked.conversation_id)
        if grant_approved and parked.conversation_id is not None
        else []
    )
    decisions: dict[str, ToolApproved | ToolDenied] = {}
    for call_id, outcome in parked.settled.items():
        # isinstance rather than the set above: the re-check needs the grant's width, which
        # only the narrowed type carries.
        if not isinstance(outcome, GrantApproved):
            # Settled on grounds no grant touches, so nothing to re-validate: a refusal by
            # the permission level (a later grant covers a tool the level forbids anyway);
            # a call Auto's review cleared on its own grounds; and an approval the top level
            # gave outright (a Yolo turn parked on a question, ``agent/turn.py``). None
            # leaves a grant behind, so a grant check would misread that as revocation. A
            # review that cleared a call because of a grant is marked as the grant's
            # (``agent/gating.py``) and is re-checked.
            decisions[call_id] = outcome
        elif (call := call_by_id.get(call_id)) is not None and _still_covered(
            call, outcome, active
        ):
            decisions[call_id] = ToolApproved()
        else:
            # Revoked or expired while parked; the message names neither cause.
            decisions[call_id] = ToolDenied(
                message="This tool's conversation auto-approval is no longer in effect."
            )

    # New "allow for this conversation" grants chosen this batch, as (tool, command scope,
    # decisive) rows. At the narrow width the scope is derived here from the call as it will
    # run (the operator's override if any, since that is the act approved), never sent by the
    # client, which could otherwise widen it. A command tool yields one grant per stage of
    # its command line, none if unreadable (`services/approval_grants.py`); those ids go
    # back in `unscoped`. The wider width names the tool, so it derives nothing and never
    # appears there.
    to_grant: list[tuple[str, tuple[str, ...], bool]] = []
    unscoped: list[str] = []
    for decision in body.decisions:
        if decision.approved:
            decisions[decision.tool_call_id] = ToolApproved(override_args=decision.override_args)
            granted_call = call_by_id[decision.tool_call_id]
            # A guarded or ungrantable call is approved but its scope is silently not honoured
            # (not a 400, not `unscoped`): the client was right to offer the checkbox.
            if (
                decision.scope != "once"
                and parked.conversation_id is not None
                and decision.tool_call_id not in guarded
                and grantable(
                    granted_call.tool_name,
                    parked.requests.metadata.get(decision.tool_call_id),
                )
            ):
                if decision.scope == "conversation_tool":
                    # Nothing to derive: even an unreadable command is grantable at this width.
                    scopes: list[tuple[str, ...]] | None = [()]
                else:
                    effective = (
                        decision.override_args
                        if decision.override_args is not None
                        else granted_call.args_as_dict()
                    )
                    scopes = grant_scopes(granted_call.tool_name, effective)
                    if scopes is None:
                        unscoped.append(decision.tool_call_id)
                decisive = decision.scope == "conversation_tool"
                for scope in scopes or ():
                    row = (granted_call.tool_name, scope, decisive)
                    if row not in to_grant:
                        to_grant.append(row)
        else:
            decisions[decision.tool_call_id] = ToolDenied(
                message=refusal_message(decision.intent, decision.message)
            )
            # The plan stays on screen and must stop saying it awaits an answer; only its
            # status moves, since a revision is written against it.
            if call_by_id[decision.tool_call_id].tool_name == PLAN_SUBMIT_TOOL and (
                conversation := parked.conversation_id
            ):
                _ = await deps.plan_mode(request).settle(
                    deps.OPERATOR_ID,
                    conversation,
                    "revising" if decision.intent == "revise" else "denied",
                    run=run,
                )

    # Decided now, so resolve the park's notification rather than waiting for terminal.
    # Idempotent.
    _ = await deps.notifications(request).resolve_for_run(deps.OPERATOR_ID, run_id)

    # A park restored after a restart holds no agent yet; its kind's composer rebuilds it.
    # When what it ran on is gone, no pending call runs and the run ends blocked.
    try:
        _ = await recompose(registry, run, parked)
    except RecomposeError as exc:
        reason = (
            f"This run could not be resumed after the restart ({exc}), "
            + "so its pending calls were denied."
        )
        _ = await registry.block_parked(run_id, reason)
        raise HTTPException(status_code=409, detail=reason) from exc

    # The park's deps hold the agent's own withheld tools; nothing else here re-derives them.
    restored_disabled: frozenset[str] = (
        parked.compaction.deps.disabled_tools if parked.compaction is not None else frozenset()
    )
    orchestrator = build_resume_orchestrator(
        parked,
        decisions,
        answers=answers,
        # The bag the original turn ran with; resume is the only path approval-gated tools run on.
        capabilities=deps.capabilities(request),
        store=deps.store(request),
        # The operator's disabled set and current offline state apply on resume, so a tool
        # switched off while parked stays hidden; a gate missing here would miss exactly the
        # approval-gated calls. The level and workspace kind reach the gate through `RunDeps`
        # (`build_resume_orchestrator`), see `routes/deps.py`.
        disabled_tools=await deps.disabled_tools(
            request,
            # The run's kind, so a resumed unattended turn doesn't regain attended-only tools.
            kind=run.kind,
        )
        | restored_disabled,
    )
    # Recorded before resuming, which only schedules the turn: its inline grant check must
    # see them or a tool re-called in that turn would re-prompt. If the resume is refused,
    # the new grants are rolled back so a dead run leaves no standing auto-approval.
    #
    # Only new ones are rolled back, hence reading existing scopes first: at the reviewing
    # level a command-scoped grant doesn't settle a call, so a thread can hold a live grant
    # and still park on the act it names. Approving refreshes that row, and a blanket
    # rollback would delete a grant held before this card opened.
    conv_id = parked.conversation_id
    already: set[tuple[str, tuple[str, ...]]] = set()
    if to_grant and conv_id is not None:
        already = {
            (g.tool_name, g.command_prefix) for g in await grants.list(deps.OPERATOR_ID, conv_id)
        }
        for tool_name, scope, decisive in to_grant:
            _ = await grants.grant(deps.OPERATOR_ID, conv_id, tool_name, scope, decisive)
    # Resume runs under the operator's bounds, not registry defaults, or the setting would be
    # a half-truth on the only path approval-gated tools run on.
    settings_store = deps.settings_store(request)
    if (
        await registry.resume(
            run_id,
            orchestrator,
            inactivity_timeout_s=await get_inactivity_timeout(settings_store, deps.OPERATOR_ID),
            wall_clock_timeout_s=await get_wall_clock_timeout(settings_store, deps.OPERATOR_ID),
        )
        is None
    ):
        if to_grant and conv_id is not None:
            for tool_name, scope, _ in to_grant:
                if (tool_name, scope) not in already:
                    await grants.revoke(deps.OPERATOR_ID, conv_id, tool_name, scope)
        raise HTTPException(status_code=409, detail="run could not be resumed")
    if conv_id is not None:
        await deps.approval_ledger(request).record(
            deps.OPERATOR_ID,
            conv_id,
            [
                LedgerEntry.of(
                    decision.tool_call_id,
                    call_by_id[decision.tool_call_id].tool_name,
                    decision.override_args
                    if decision.override_args is not None
                    else call_by_id[decision.tool_call_id].args_as_dict(),
                    "operator",
                )
                for decision in body.decisions
                if decision.approved
            ],
            keep=get_settings().review_ledger_entries,
        )
    return ApprovalOutcome(granted=[list(scope) for _, scope, _ in to_grant], unscoped=unscoped)
