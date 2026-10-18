"""Substrate HTTP surface: observe, stream, and cancel a Run.

These are the transport-level endpoints every orchestrator inherits. Creating a
Run is a feature concern (the chat and agent routes), not here — those call
``deps.registry(request).submit(...)`` and hand the client back the run id.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field
from pydantic_ai import ToolApproved, ToolDenied
from pydantic_ai.messages import ToolCallPart

from agent import ParkedTurn, build_resume_orchestrator
from agent.gating import GrantApproved
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
from services.answers import (
    AnswerError,
    answered_questions,
    questions_of,
    render_answer,
)
from services.approval_grants import ONCE_ONLY_TOOLS, GrantInfo, grant_scopes, grant_width
from services.approval_ledger import LedgerEntry
from services.plan_mode import PLAN_SUBMIT_TOOL
from services.settings_store import get_inactivity_timeout, get_wall_clock_timeout

router = APIRouter(prefix="/runs", tags=["runs"])


def refusal_message(intent: str, written: str | None) -> str:
    """What the model is told in place of the result, when the operator said no.

    The two are not the same instruction and a model handed the wrong one wastes the turn:
    a denial that read as "try again" produces a second attempt at something already
    refused, and a revision request that read as a flat no produces an apology and a stop.
    The operator's own words are quoted where they wrote any; the framing around them is
    what the intent decides.
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
    # The older fields serialize as snake_case. The two conversation fields serialize as
    # camelCase to match the newer surfaces (corpus, uploads, notifications).
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
    # Lets the dashboard label and link an in-flight run without a second lookup.
    conversation_id: str | None = Field(default=None, serialization_alias="conversationId")
    conversation_title: str | None = Field(default=None, serialization_alias="conversationTitle")


def _view(run: Run, title: str | None) -> RunView:
    """One run projected for the client. The conversation title is passed in rather than
    looked up here: the listing resolves every run's title in a single query, and a helper
    that reached for the store itself would put that back to one read per run."""
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
    """The operator's runs, newest first. ``active=True`` (default) returns only
    the ones still in flight (not yet terminal) — what the home page surfaces."""
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
    """SSE event stream. Reconnect with ``Last-Event-ID`` to replay missed events."""
    run = _require_run(request, run_id)
    after = parse_last_event_id(request.headers.get("last-event-id"), last_event_id)
    return sse_response(run, after)


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
    cancelled = await registry.cancel(run_id)
    if not cancelled:
        if held is not None:
            held[0].undo_stop(held[1])
        # Unknown, or already terminal — surface the distinction.
        run = registry.get(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        raise HTTPException(status_code=409, detail=f"run already {run.status.value}")
    return {"status": "cancelling"}


class ApprovalDecision(BaseModel):
    tool_call_id: str
    approved: bool
    message: str | None = None  # shown to the model on denial
    override_args: dict[str, object] | None = None  # replace args on approval
    # How long this yes lasts. "once" is this call only. "conversation" records an
    # auto-approval grant so the same *act* isn't re-prompted for the rest of the thread —
    # which on a tool that runs a command is that command, derived here from the parked
    # call. "conversation_tool" is the operator's wider pick: the whole tool, everything it
    # runs, offered on the card in its own words because it is a materially larger thing to
    # say and the narrow one must stay the easy path.
    scope: Literal["once", "conversation", "conversation_tool"] = "once"
    # Which kind of no this is. Read only when `approved` is False, and it changes nothing
    # about what runs — both stop the call — only what the model is told happened. "deny"
    # is the operator refusing the act; "revise" is them asking for a different version of
    # it, which is a materially different instruction and the one a model most often gets
    # wrong on a bare refusal. Generic rather than plan-specific: any deferred call can be
    # sent back for another attempt, and the surface that most needs it (a submitted plan)
    # is only the first to offer it.
    intent: Literal["deny", "revise"] = "deny"


class QuestionReply(BaseModel):
    """What the operator said to one question."""

    # The labels they chose, exactly as offered. Labels rather than indices so a reply
    # cannot silently land on the wrong option if the two sides ever disagree about
    # order — and the server checks them against the parked call either way.
    selections: list[str] = Field(default_factory=list)
    # What they wrote instead of, or in addition to, choosing. Always available: the
    # options are the model's guesses at the answer, not the range of possible answers.
    text: str | None = None


class QuestionAnswer(BaseModel):
    """One parked ``ask_user`` call, answered. ``replies`` is positional — one per
    question in the call, in the order they were asked."""

    tool_call_id: str
    replies: list[QuestionReply]


class ApprovalDecisions(BaseModel):
    decisions: list[ApprovalDecision] = Field(default_factory=list)
    answers: list[QuestionAnswer] = Field(default_factory=list)


class ApprovalOutcome(BaseModel):
    """What settling the park did — the resume, and what became of the standing yeses.

    The second half is here because it can differ from what was asked for, silently and
    for a good reason. A "for this conversation" opt-in on a command no scope could stand
    for records **nothing** (`services/approval_grants.py`), and a client that assumed
    otherwise would leave the operator with a ticked box, no chip on the strip and no
    account of why. So the route says what it wrote and which calls it could not write,
    and the client is left with something true to say.
    """

    status: Literal["resuming"] = "resuming"
    #: The scopes recorded by this call, as the words each names — `[]` for a whole-tool
    #: grant, and the whole list empty when nothing was asked for.
    granted: list[list[str]] = Field(default_factory=list)
    #: The calls whose conversation opt-in could not be scoped, by tool call id. The
    #: approval itself still stands; only the standing part of it was refused.
    unscoped: list[str] = Field(default_factory=list)


def _still_covered(call: ToolCallPart, settled: GrantApproved, active: Sequence[GrantInfo]) -> bool:
    """Whether the grant that settled this call still covers it, **at the width it needed**.

    A grant can be revoked, or lapse, while the run waits — so an allow made without the
    operator is re-checked before the turn resumes. What it is re-checked *against* is the
    part this has to get right, because the two widths stopped buying the same thing: only
    a whole-tool grant clears an act nobody can undo
    (:func:`services.permissions.decide.review`).

    That one allow is re-checked at the wider width and nothing less. Asking only "is this
    covered at all" would, on a command-scoped tool, let a surviving grant over one command
    stand in for the revoked whole-tool one — and that narrower grant is precisely the one
    the risk axis refused. Every other grant-driven allow keeps the question it always had:
    covered by a grant of either width, since either is what produced it.
    """
    width = grant_width(call.tool_name, call.args_as_dict(), active)
    return width == "tool" if settled.needs_whole_tool else width != "none"


@router.post("/{run_id}/approve", status_code=202)
async def approve_run(run_id: str, body: ApprovalDecisions, request: Request) -> ApprovalOutcome:
    """Settle everything a parked run is awaiting — permissions and answers — then
    resume it.

    One endpoint for both because a run parks once and resumes once: a turn that stopped
    on an approval *and* a question is waiting on a single set, and a client that could
    send half of it would leave the other half with no way to arrive.
    """
    registry = deps.registry(request)
    run = registry.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    if run.status is not RunStatus.awaiting_input or not isinstance(run.parked_payload, ParkedTurn):
        raise HTTPException(status_code=409, detail=f"run is not awaiting approval ({run.status})")

    parked: ParkedTurn = run.parked_payload
    # The operator only decides the calls that weren't already settled without them — by
    # an active conversation grant, or by the thread's permission level refusing outright.
    # Those ride on the parked payload and merge back below.
    call_by_id = {call.tool_call_id: call for call in parked.requests.approvals}
    pending = set(call_by_id) - set(parked.settled)
    # The questions are pending too, and unconditionally: nothing settles one ahead of
    # the operator. Both piles are checked against one set because the run resumes once —
    # a body covering only the approvals would resume a turn whose question still has no
    # answer, and Pydantic AI would have nothing to hand that call.
    asked = {call.tool_call_id: call.args_as_dict() for call in parked.requests.calls}
    # Each pile is checked against *its own* ids, not just against the union. A body
    # that answered an approval — or decided a question — would satisfy a union check
    # while leaving the call it named with the wrong kind of result: the two piles are
    # settled by different code below, and each would then index the map the id is not
    # in. Checked separately, a swapped id is a 400 rather than a 500.
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

    # The answers, rendered from the *parked arguments* rather than from anything the
    # client sent as prose. The client says which options it chose; what those options
    # said is read back off the call the model actually made, so the transcript and the
    # model cannot be shown two different questions.
    answers: dict[str, str] = {}
    # The same reply as structure, for the card the client draws beside the transcript.
    # Built from the same two inputs the prose is — the parked call's questions and the
    # replies just validated against them — because a client that had to read the answer
    # back out of the prose would be parsing a format it does not own.
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
    # Emitted only once every answer has rendered: a body whose second answer is refused
    # settles nothing, so a frame for its first would announce an answer the run never got.
    for frame in announced:
        _ = run.emit(frame)

    grants = deps.approval_grants(request)
    # Re-validate the grant-driven pre-approvals against the *current* grants: a grant
    # the operator revoked — or that lapsed by TTL — while the run was parked must not
    # still auto-run its call.
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
        # The same `isinstance` the set above is built from, rather than a lookup in it:
        # the re-check below needs the grant's *width* off the decision, which only the
        # narrowed type carries.
        if not isinstance(outcome, GrantApproved):
            # Settled on grounds a grant has nothing to do with, and so nothing to
            # re-validate against. Three of them reach here: a refusal the permission
            # level made when the turn parked (the operator was never asked, and a grant
            # recorded since covers a tool the level does not permit at all); a call
            # Auto's review cleared on its own grounds; and an **approval the level gave
            # outright** at the top level, where nothing is asked and the grants are never
            # consulted — a Yolo turn reaches this path when it parks on a *question*
            # rather than an approval (``agent/turn.py``). None of the three leaves a
            # grant behind, so all three would be denied by a grant check that took that
            # silence for a revocation. A review that cleared a call *because* of a grant
            # is marked as the grant's (``agent/gating.py``) and so is not in this branch:
            # what cleared it is revocable, and is re-checked.
            decisions[call_id] = outcome
        elif (call := call_by_id.get(call_id)) is not None and _still_covered(
            call, outcome, active
        ):
            decisions[call_id] = ToolApproved()
        else:
            # The grant was revoked or expired while parked; either way it no longer
            # covers the call, so don't assert a cause the transcript can't stand behind.
            decisions[call_id] = ToolDenied(
                message="This tool's conversation auto-approval is no longer in effect."
            )

    # New "allow for this conversation" grants the operator chose this batch, as
    # (tool, command scope, decisive) rows. At the narrow width the scope is derived
    # **here, from the call as it will actually run** rather than sent by the client: what
    # the operator is saying yes to is
    # the act about to be taken, and a scope the client could name is a scope it could
    # widen. That is the call's own arguments, or the ones the operator replaced them with
    # — an override *is* the act they approved, and scoping the standing yes to the command
    # they edited away would leave a grant behind for something nobody is going to run. A
    # command-running tool contributes one grant per stage of its command line, and none at
    # all when the command could not be read (`services/approval_grants.py`); the ids of
    # those ride back on the response, since the operator asked for something they did not
    # get. The wider width derives nothing — it names the tool, which the parked call
    # already says — so it cannot fail that way and never appears in `unscoped`.
    to_grant: list[tuple[str, tuple[str, ...], bool]] = []
    unscoped: list[str] = []
    for decision in body.decisions:
        if decision.approved:
            decisions[decision.tool_call_id] = ToolApproved(override_args=decision.override_args)
            granted_call = call_by_id[decision.tool_call_id]
            # A once-only tool's call is approved; its *scope* is not honoured — and it is
            # not reported as unscoped either, since nothing about the call is what stopped
            # it from being granted. Silently rather than as a 400: the client offering the
            # checkbox is not wrong to have offered it, and the operator's decision on this
            # call still stands.
            if (
                decision.scope != "once"
                and parked.conversation_id is not None
                and granted_call.tool_name not in ONCE_ONLY_TOOLS
            ):
                if decision.scope == "conversation_tool":
                    # The wider pick names the tool, so there is nothing to derive and
                    # nothing that can fail to derive: a command this walk cannot read is
                    # still grantable at this width, because the operator is not saying
                    # anything about the command. It is never reported as unscoped for the
                    # same reason — they got exactly what they asked for.
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
            # A refused plan is the one refusal that leaves a record behind: the document
            # is still on screen, and it must not go on saying it is waiting for an answer
            # that has arrived. The status is all that moves — the plan itself stays, since
            # a revision is written against it and a rejection is worth still being able to
            # read.
            if call_by_id[decision.tool_call_id].tool_name == PLAN_SUBMIT_TOOL and (
                conversation := parked.conversation_id
            ):
                _ = await deps.plan_mode(request).settle(
                    deps.OPERATOR_ID,
                    conversation,
                    "revising" if decision.intent == "revise" else "denied",
                    run=run,
                )

    # The approval is decided now, one way or another (approved, denied, or already
    # grant-covered) — resolve the park's notification here rather than waiting for the
    # run to reach terminal. Idempotent: a run with no pending approval_needed (or one
    # already resolved) is simply a no-op.
    _ = await deps.notifications(request).resolve_for_run(deps.OPERATOR_ID, run_id)

    orchestrator = build_resume_orchestrator(
        parked,
        decisions,
        answers=answers,
        # The same app-wide agent-facing bag the original turn ran with — a resumed
        # turn re-runs the approved tool call, and mail-send/vault-read/external are
        # the approval-gated ones, so this resume path is the *only* way they ever run.
        capabilities=deps.capabilities(request),
        store=deps.store(request),
        # A resumed turn respects the operator's disabled set and the current offline
        # state — a tool switched off (or a link dropped) while it was parked stays hidden
        # on resume. This is the only path an approval-gated tool ever actually runs on,
        # so a gate missing here would be a gate that never applies to the calls that
        # matter most.
        # The level and the workspace kind reach the gate through `RunDeps`
        # (`build_resume_orchestrator`), not through this set — see `routes/deps.py`.
        disabled_tools=await deps.disabled_tools(
            request,
            # ...and the run's own kind, so a resumed unattended turn is offered the same
            # stack it parked with rather than gaining the attended-only tools back on
            # the one path that continues it.
            kind=run.kind,
        ),
    )
    # Record grants *before* resuming: resume only schedules the turn (it doesn't await
    # it), and the resumed turn's inline grant check must see them, or a tool re-called
    # within that same turn would re-prompt despite the operator's "allow for this
    # conversation". If the resume can't be accepted, roll the new grants back so a dead
    # run leaves no standing auto-approval behind.
    #
    # **Only the ones that are new**, which is why the scopes already there are read first.
    # The rollback used to assume a pending call meant no grant covered it — true where a
    # level asks outright, and false at the level that reviews: there a command-scoped
    # grant does not settle a call, so a thread can hold a live standing yes and still park
    # on the very act it names. Approving that refreshes the row rather than creating one,
    # and a blanket rollback would then delete a grant the operator had before they ever
    # opened this card, because a run they were not told about failed to resume.
    conv_id = parked.conversation_id
    already: set[tuple[str, tuple[str, ...]]] = set()
    if to_grant and conv_id is not None:
        already = {
            (g.tool_name, g.command_prefix) for g in await grants.list(deps.OPERATOR_ID, conv_id)
        }
        for tool_name, scope, decisive in to_grant:
            _ = await grants.grant(deps.OPERATOR_ID, conv_id, tool_name, scope, decisive)
    # A resumed turn runs under the *operator's* bounds, not the registry defaults. Both
    # are settings now, and a continuation that quietly ran unbounded (or under a config
    # default the operator had overridden) would make the setting a half-truth — approval
    # resume is the only path an approval-gated tool ever actually runs on.
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
