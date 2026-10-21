"""Ruling on the calls a turn deferred — the engine's half of the decision.

A model turn that wants a sensitive tool ends with the call *unexecuted* and the question
open: does this run, does it get refused, or does it go to the operator? Answering it is
this module's whole job. :func:`settle_deferred` walks the batch and returns the two piles
the turn continues on — the calls settled without a human, and the ones that need one —
so ``turn.py`` is left with control flow rather than policy.

``services/permissions`` owns the rules: what an action reaches, whether its structure
clears it, and what a model's three scores add up to. None of that knows about runs,
streams or capability bags, and it should not. This module is the seam between the two: it
resolves the reviewer from the run's capabilities, hands the rules everything they need —
including the fact about *this host* the structural stage cannot look up for itself,
whether a fence can be built — and announces what happened on the run's own event stream.

**Why the announcement is not optional.** Auto's whole proposition is that the operator's
approvals are given for them. That is only acceptable if it is *visible* — so a reviewed
call emits ``review.started`` before it is ruled on and ``review.completed`` after, and
those land in the work log beside the call they judged. Without them the operator sees a
tool call they never approved and no account of why it ran, which is indistinguishable
from the gate having silently failed open.

**These two frames are the Auto level's own, and they are not where the reach and the
fence are reported from.** Only a reviewing level produces them, so a command at Plan,
Manual, Edit or Yolo would have carried no account of what held it. The declared reach and
the fence's verdict therefore ride the executing tool's **result** as well
(``tools/shell.py``), which is true at every level and is what a cold reload reads. What
these frames add on top is the *ruling* — who cleared the call and on what ground — which
is a fact about a review and exists nowhere else.

**One review pass per batch, and it runs concurrently.** A model turn can defer several
calls at once, and each is judged on its own — the deterministic stage may clear three and
send the fourth to the model — but everything a review needs that is *not* per-call is
built once for the batch: the reviewer (a registry resolution and a model construction),
the transcript (a walk of the recent history into role-tagged entries the reviewer reads
inside an untrusted fence, never as lines it could be talked into believing) and the
home every path is measured against (:func:`_judged_root`). The model calls
that remain are independent of one another and each carries a timeout measured in
seconds, so they are awaited together rather than in a line; a turn that deferred four
calls waits once, not four times.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from pydantic_ai import ToolApproved, ToolDenied
from pydantic_ai.messages import ModelMessage, ToolCallPart

from core.concurrency import gather_bounded
from core.config import get_settings
from core.container import ServiceContainer
from core.exceptions import DegradedCapabilityError, NotFoundError
from core.untrusted import new_nonce
from runs import Run
from runs.events import ReviewCompleted, ReviewStarted
from services.approval_grants import (
    ApprovalGrantStore,
    GrantInfo,
    covered_by_grant,
    grant_width,
)
from services.home.table import RunHome
from services.permissions import (
    Decision,
    GrantWidth,
    ReviewBudget,
    Reviewer,
    ReviewOutcome,
    TranscriptEntry,
    TurnBoundary,
    blocked_message,
    capability_of,
    decide,
    make_utility_reviewer,
    measured_against_root,
    review,
    review_transcript,
)
from services.permissions.fence import Fence
from services.permissions.guards import GUARDED_TOOLS, guard, remember
from services.registry import ModelRegistry
from services.sandbox.thread_shells import ThreadShells
from services.vm.project import ProjectCopies
from tools.deps import RunDeps
from tools.home import resolve_run_home

logger = logging.getLogger(__name__)

#: How many reviews of one batch may be in flight at once. Each is a utility-model round
#: trip, so the cap protects the far side (and a local runtime's own queue) rather than
#: this process; a turn rarely defers more than a handful of calls, and the point is that
#: their latencies overlap rather than stack.
_REVIEW_CONCURRENCY = 4


@dataclass(kw_only=True)
class GrantApproved(ToolApproved):
    """An approval a standing conversation grant produced, marked as one.

    The approve route re-validates what a parked turn settled without the operator, because
    a grant can be revoked — or lapse by TTL — while the run waits. Only a *grant's*
    approval has anything to re-validate, though: a review that cleared a call on its own
    grounds leaves no grant behind, so re-checking it against the grants comes back
    uncovered and denies a call the operator was never offered and never refused.

    **A review can still produce one.** At Auto a grant is an input rather than the answer,
    but where it is what supplied the authorization the allow rests on the same revocable
    thing an Edit-level grant approval does (``ReviewOutcome.by_grant``) — so it is marked
    the same way and re-checked the same way when the operator answers the rest of the
    batch.

    The mark rides on the decision rather than beside it because the decision is the only
    part of the settled pile that crosses into the parked payload, and a provenance kept
    anywhere else would have to be carried through by hand at every hop that touches it.

    ``needs_whole_tool`` is what the re-check has to *demand*, and it is deliberately not
    the width the operator happened to be holding. Only one allow turns on that width —
    the one where it cleared an act nobody can undo
    (``services/permissions/decide.py``) — and only that one may be denied when a narrower
    grant is all that survives a revocation. Recording the held width instead would break
    Manual and Edit, where either width settles the call outright: a thread holding both a
    whole-tool grant and a command one would measure "tool", and revoking only the former
    would then deny a call the surviving command grant still fully answers for.
    """

    #: Whether this allow rests on the *wider* width specifically. False for every allow a
    #: grant of either width could have produced, which is all of them but one.
    needs_whole_tool: bool = False


async def settle_deferred(
    run: Run,
    approvals: Sequence[ToolCallPart],
    *,
    caps: ServiceContainer,
    conversation_id: str | None,
    deps: RunDeps,
    messages: list[ModelMessage],
    permission: str,
    turn_start: TurnBoundary | None = None,
    budget: ReviewBudget | None = None,
) -> tuple[dict[str, ToolApproved | ToolDenied], list[ToolCallPart]]:
    """Rule on every call this hop deferred, returning ``(settled, manual)``.

    ``settled`` is the decisions the turn can carry on with immediately — an allow the
    model never notices, or a denial it is told in place of the tool's result and re-plans
    around. ``manual`` is what is left for the operator, and a non-empty one is what parks
    the turn.

    **A guarded call is ruled before any of that** (``services/permissions/guards.py``): it
    goes to the operator at every level, and neither a grant nor the reviewer can answer it.
    A level that refuses outright still refuses.

    **The level rules next, and a grant may answer it but never overturn it.** A standing
    grant is the operator's "stop asking me about this one", so it settles a call the level
    would have put in front of them. But a level that refuses outright carries their answer
    already, in the level they chose — a grant recorded while a thread could act is not
    consent to act in one they have since set to act in nothing, and Plan's whole contract
    is that nothing changes. The resume path reads it the same way (``routes/runs.py``): a
    denial the level made carries forward verbatim, and no grant undoes it. Grants are
    conversation-scoped, so a stateless turn always asks — and a grant on a tool that runs
    a command is scoped to the command's leading words, so a standing yes to `uv run
    pytest` settles the next test run and nothing else the shell could be asked to do.

    **At the top level the grants are not consulted at all**, for the opposite reason to
    Plan's: nothing is asked, so there is no prompt for a standing yes to skip and no park
    for a revocation to catch. The approval that comes out is the level's rather than a
    grant's, and is recorded as such (:func:`_by_level`).

    **At Auto the grant is not the answer either — it is an input to the review**, and how
    much of an input depends on which width the operator chose. The level's question there
    is what a particular call would do, and a grant naming one command is not an answer to
    that for every command in the thread: it rides along as the authorization for the act
    it names and the review still runs. The wider grant — the one offered in its own words,
    this tool and everything it runs — *is* read as that answer, because it is the
    operator's reply to the same question the review is asking, and it is the one thing
    that also clears an act nobody can undo. Neither is a way past the judge, and neither
    stands in for a review that could not run at all, which parks whatever the grants say
    (:func:`services.permissions.review`). An allow the grant's authorization is what
    produced is recorded as the grant's (:class:`GrantApproved`), so the resume path
    re-checks it if the operator revokes while the rest of the batch waits.

    ``turn_start`` and ``budget`` belong to the *turn* rather than to this batch: the first
    is where the turn's own messages begin, so the reviewer can be given the request that
    opened it however many tools have run since, and the second is the ceiling on how many
    model reviews one turn may spend. Both are threaded from ``drive_turn``, which owns
    everything a turn shares across its segments.
    """
    granted: list[GrantInfo] = []
    store = caps.get_optional(ApprovalGrantStore)
    if store is not None and conversation_id is not None:
        granted = await store.list(run.owner_id, conversation_id)
    # Ruled on before anything is reviewed, so the batch knows which calls need a model at
    # all before it pays to resolve one. Each row carries the decision *and* whether a
    # grant is what produced it, because the two are not recoverable from each other: an
    # ALLOW can come from a grant or from a level that allows outright, and only the first
    # is revocable.
    home = await _guard_home(deps, approvals)
    guarded = _guard_reasons(run, approvals, home)
    ruled = [
        (call, Decision.ASK, False)
        if call.tool_call_id in guarded and decide(permission, call.tool_name) is not Decision.BLOCK
        else (call, *_by_level(permission, call, granted))
        for call in approvals
    ]
    reviewed = await review_batch(
        run,
        [call for call, decision, _ in ruled if decision is Decision.REVIEW],
        caps=caps,
        deps=deps,
        messages=messages,
        grants=granted,
        turn_start=turn_start,
        budget=budget,
        home=home,
    )
    settled: dict[str, ToolApproved | ToolDenied] = {}
    manual: list[ToolCallPart] = []
    for call, decision, grant_backed in ruled:
        outcome = reviewed.get(call.tool_call_id)
        if outcome is not None:
            decision = outcome.decision
        if decision is Decision.ALLOW:
            # Reviewed, resting on a grant, or given by a level that asks nobody — and
            # which of the three is recorded, because only the grant is still worth
            # re-checking when the operator answers. A review that leant on the grant for
            # its authorization counts as the grant's: what cleared the call is revocable,
            # so the resume path has to see that. A level's own allow is **not** the
            # grant's, and marking it so would be a false provenance the resume path takes
            # literally — it would look for a grant covering the call, find none, and deny
            # something no operator had refused.
            by_grant = outcome.by_grant if outcome is not None else grant_backed
            settled[call.tool_call_id] = (
                GrantApproved(
                    # Reaching here with no ``outcome`` means a grant settled the call at
                    # a level that asks, and those take either width — so it demands
                    # neither back. Only a review can require the wider one, which is why
                    # the flag is read off the outcome and nowhere else.
                    needs_whole_tool=outcome is not None and outcome.needs_whole_tool
                )
                if by_grant
                else ToolApproved()
            )
        elif decision is Decision.BLOCK:
            # Only a level refuses outright; the review's own refusals became parks, since
            # an act nobody can undo is the one the operator most needs to be shown.
            settled[call.tool_call_id] = ToolDenied(
                message=blocked_message(permission, call.tool_name)
            )
        else:
            manual.append(call)
    return settled, manual


async def _guard_home(deps: RunDeps, approvals: Sequence[ToolCallPart]) -> RunHome | None:
    """The home the guard places paths against, opened only when a call in the batch is
    one it has a rule for."""
    if not any(call.tool_name in GUARDED_TOOLS for call in approvals):
        return None
    try:
        return await resolve_run_home(deps)
    except Exception:  # a guard that raises would abort the operator's turn
        logger.info("guard: no home to place paths against", exc_info=True)
        return None


def _guard_reasons(
    run: Run, approvals: Sequence[ToolCallPart], home: RunHome | None
) -> dict[str, str]:
    """Why each call that must go to the operator at every level has to, by call id.

    Ahead of the grants, the level and the reviewer: none of them may answer these. The
    reasons are handed to the park that announces them (:func:`guards.remember`).
    """
    candidates = [call for call in approvals if call.tool_name in GUARDED_TOOLS]
    settings = get_settings()
    reasons: dict[str, str] = {}
    for call in candidates:
        reason = guard(call.tool_name, call.args_as_dict(), home=home, settings=settings)
        if reason is not None:
            reasons[call.tool_call_id] = reason
            remember(run.id, call.tool_call_id, reason)
    return reasons


def _by_level(
    permission: str, call: ToolCallPart, granted: Sequence[GrantInfo]
) -> tuple[Decision, bool]:
    """What the thread's level says about one call, with the operator's standing grants
    allowed to answer — but only where the level was asking a question they can answer.

    Returns the decision and **whether a grant is what produced it**, which the caller
    cannot work out from the decision alone once more than one thing can return ``ALLOW``.

    Three of the level's answers stand whatever the grants say. A refusal already carries
    the operator's answer in the level they chose. A **review** is not a prompt to be
    skipped: it is the level doing the deciding, and a grant that short-circuited it would
    turn one "allow for this conversation" on a shell tool into a thread where no command
    is ever looked at again — the grant is handed to the review instead. And an **allow**
    has nothing left for a grant to add: at the top level nothing is asked at all, so the
    grants are not consulted and the resulting approval is the level's rather than
    theirs. Recording it as a grant's would be worse than merely inaccurate — the resume
    path re-checks a grant-backed approval against the live grants, and would deny a call
    that never rested on one.

    The *call*, not just its name, because a grant on a command-running tool is scoped to
    the command (``services/approval_grants.py``): what settles a call at Manual and Edit
    is a standing yes to this act, not to everything that tool could be asked to do.
    """
    decision = decide(permission, call.tool_name)
    if decision in (Decision.BLOCK, Decision.REVIEW, Decision.ALLOW):
        return decision, False
    if covered_by_grant(call.tool_name, call.args_as_dict(), granted):
        return Decision.ALLOW, True
    return decision, False


async def review_batch(
    run: Run,
    calls: Sequence[ToolCallPart],
    *,
    caps: ServiceContainer,
    deps: RunDeps,
    messages: list[ModelMessage],
    grants: Sequence[GrantInfo] = (),
    turn_start: TurnBoundary | None = None,
    budget: ReviewBudget | None = None,
    home: RunHome | None = None,
) -> dict[str, ReviewOutcome]:
    """Review every call the level sent to review, by ``tool_call_id``.

    ``home`` is the one the caller already opened for the batch, so it is not opened twice.

    The reviewer and the transcript are built once for the batch and shared: resolving a
    reviewer is a registry lookup and a model construction, and the transcript is the same
    walk over the same recent history for every call in the turn — rebuilt per call it was
    several kilobytes of identical string per deferred tool.

    **The fence token is minted once here too, and that is what makes the sharing pay.** The
    transcript being one object saved this process the work of rebuilding it; it saved the
    *server* nothing, because each prompt still opened on a nonce of its own and so diverged
    from its siblings within a few tokens. One nonce per batch, with the shared prose first
    (``reviewer.review_prompt``), means the second and later reviews of a turn arrive on a
    prefix the engine has already processed. Per batch and no wider: a fresh token every
    turn, never reused across threads or over time.
    """
    if not calls:
        return {}
    settings = get_settings()
    reviewer = await resolve_reviewer(caps, run.owner_id)
    transcript = review_transcript(
        messages, turn_start=turn_start, limit=settings.review_transcript_entries
    )
    nonce = new_nonce()
    root = await _judged_root(deps, calls, home)
    # Resolved once for the batch: a non-host command is held to its reach only by running
    # in the platform VM, and the shell refuses every command while the VM is down.
    fenced = vm_fences(caps)
    outcomes = await gather_bounded(
        [
            review_call(
                run,
                tool_call_id=call.tool_call_id,
                tool=call.tool_name,
                args=call.args_as_dict(),
                root=root,
                transcript=transcript,
                reviewer=reviewer,
                granted=grant_width(call.tool_name, call.args_as_dict(), grants),
                fenced=fenced,
                budget=budget,
                nonce=nonce,
            )
            for call in calls
        ],
        _REVIEW_CONCURRENCY,
    )
    return {call.tool_call_id: outcome for call, outcome in zip(calls, outcomes, strict=True)}


def vm_fences(caps: ServiceContainer) -> bool:
    copies = caps.get_optional(ProjectCopies)
    return copies is not None and copies.vm.available


async def _judged_root(
    deps: RunDeps, calls: Sequence[ToolCallPart], opened: RunHome | None = None
) -> Fence | None:
    """The home every path in this batch is measured against, read from where the
    thread's shell stands.

    **Resolved, not read off the run's memo**, and that is the whole point: the *first*
    shell command of a turn is deferred before any tool has run, so nothing has opened a
    workspace yet and the memo is empty. Judging against None is the strictest reading —
    every absolute or upward path escapes — but on the one call it matters for it is also
    the *wrong* reading, and it escalated ordinary work that a resolved root clears.

    Once for the batch, and only when a call in it is actually placed against a root
    (:func:`measured_against_root`). Opening a workspace is a `git worktree add` or a
    sandbox start; every call in the batch would be judged against the same directory
    anyway, and a batch of mail sends would pay for one to learn nothing.

    A failure here never ends the turn. The workspace this could not open is the one the
    approved call would have needed, so the tool will report it in words the model can act
    on; the judge's job in the meantime is to answer, and with no root it answers strictly.
    """
    if not any(measured_against_root(call.tool_name) for call in calls):
        return None
    home = opened
    if home is None:
        try:
            home = await resolve_run_home(deps)
        except Exception:  # a judge that raises would abort the operator's turn
            logger.info("auto review: no home to judge against", exc_info=True)
            return None
    if home is None:
        return None
    shells = deps.caps.get_optional(ThreadShells)
    cwd = shells.cwd(deps.workspace_key) if shells is not None else None
    # A working directory in a place the home no longer holds is where the shell will not
    # start (`HomeShell.stand_within`).
    return home.fence(cwd if cwd is not None and home.place_of(cwd) is not None else None)


async def resolve_reviewer(caps: ServiceContainer, owner_id: str) -> Reviewer | None:
    """The Auto reviewer for this run, or None when there is nothing to review with.

    Resolves the **utility** role through the same ``resolve_background`` rule titling,
    verification and delegation use, so the review is cheap by construction rather than by
    a second policy — and so an operator who has bound only ``main`` still gets one.

    None is the degraded answer, and every caller turns it into a park. That is the one
    place in this codebase where the conservative branch has to be the default: a review
    that cannot run is not a review that passes.
    """
    registry = caps.get_optional(ModelRegistry)
    if registry is None:
        return None
    try:
        resolved = await registry.resolve_background(owner_id=owner_id)
    except (DegradedCapabilityError, NotFoundError) as exc:
        logger.info("auto review unavailable: no utility model (%s)", exc)
        return None
    except Exception:
        logger.warning("auto review unavailable: resolving a model failed", exc_info=True)
        return None
    settings = get_settings()
    return make_utility_reviewer(
        resolved.model,
        # Reasoning off for the same reason the namer and the judge request it: this is
        # background work inside a turn the operator is watching, and the three scored
        # fields are read off the structured output whatever the model thinks first.
        model_settings=resolved.reasoning_off,
        timeout_s=settings.review_timeout_s,
        max_tokens=settings.review_max_tokens,
    )


async def review_call(
    run: Run,
    *,
    tool_call_id: str,
    tool: str,
    args: dict[str, object],
    root: Fence | None,
    transcript: Sequence[TranscriptEntry],
    reviewer: Reviewer | None,
    granted: GrantWidth = "none",
    fenced: bool,
    budget: ReviewBudget | None = None,
    nonce: str | None = None,
) -> ReviewOutcome:
    """Rule on one deferred call at the Auto level, announcing both ends on the stream.

    ``root`` is the run's home, resolved once for the batch
    (:func:`_judged_root`), and None where there is none — which is not a gap but the
    strictest reading: with nowhere to measure containment against, every absolute or
    upward path in a command reads as leaving the workspace and escalates. ``fenced`` is
    the fact about the host the structural stage needs and cannot look up for itself, and
    ``granted`` how far the operator's standing grant reaches over this call — nothing, the
    one act it was ticked under, or the whole tool. ``nonce`` is the
    fence token the batch shares, so every review's prompt opens on the same bytes.
    """
    capability = capability_of(tool, args, root=root)
    _ = run.emit(
        ReviewStarted(
            tool_call_id=tool_call_id,
            name=tool,
            summary=capability.summary,
            detail=capability.detail,
            reach=capability.reach,
            network=(
                capability.runs_with_network if capability.reach in ("offline", "network") else None
            ),
        )
    )
    outcome = await review(
        capability,
        reviewer=reviewer,
        transcript=transcript,
        granted=granted,
        fenced=fenced,
        budget=budget,
        nonce=nonce,
    )
    verdict = outcome.verdict
    _ = run.emit(
        ReviewCompleted(
            tool_call_id=tool_call_id,
            name=tool,
            # `review` never returns REVIEW — it is what resolves one — so the cast to the
            # wire's three-value vocabulary is total. Spelled as a lookup rather than a
            # str() so a fourth Decision member would fail here rather than on a client.
            decision=WIRE_DECISION[outcome.decision],
            stage=outcome.stage,
            reason=outcome.reason,
            tier=outcome.tier,
            fenced=fenced,
            risk=verdict.risk if verdict else None,
            authorization=verdict.authorization if verdict else None,
            correctness=verdict.correctness if verdict else None,
        )
    )
    return outcome


#: The outcomes a review can produce, on the wire. `REVIEW` is deliberately absent: it is
#: the question, not an answer, and a review that returned it would be a bug rather than
#: a fourth case to render. `BLOCK` is the mirror image — no review returns it any more,
#: since an unrecoverable act parks instead — and it stays because the word is already in
#: the stored events of every thread that was reviewed before that changed, and a client
#: still has to render those.
WIRE_DECISION: dict[Decision, Literal["allow", "ask", "block"]] = {
    Decision.ALLOW: "allow",
    Decision.ASK: "ask",
    Decision.BLOCK: "block",
}
