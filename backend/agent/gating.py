"""Ruling on the calls a turn deferred: the engine's half of the decision.

A turn wanting a sensitive tool ends with the call unexecuted; :func:`settle_deferred`
walks the batch and returns the calls settled without a human and those needing one, so
``turn.py`` keeps control flow, not policy.

``services/permissions`` owns the rules (what an action reaches, whether its structure
clears it, how a model's three scores add up) and knows nothing of runs, streams or
capability bags. This module is the seam: it resolves the reviewer from the run's
capabilities, passes the rules the host fact the structural stage can't look up (whether
a fence can be built), and announces results on the run's stream.

The announcement is not optional. Auto gives the operator's approvals for them, which is
acceptable only if visible: a reviewed call emits ``review.started`` and ``review.completed``
into the work log, or a call they never approved would look like a gate failing open.

These frames are the Auto level's own and not where reach and fence are reported: only a
reviewing level produces them. The declared reach and fence verdict also ride the executing
tool's result (``tools/shell.py``), true at every level and what a cold reload reads. The
frames add the ruling: who cleared the call and on what ground.

One review pass per batch, run concurrently. Each deferred call is judged on its own, but
everything not per-call is built once: the reviewer, the transcript (role-tagged entries
read inside an untrusted fence) and the home paths are measured against
(:func:`_judged_root`). The remaining model calls are independent and time-bounded, so
they are awaited together.
"""

from __future__ import annotations

import logging
from collections.abc import MutableMapping, Sequence
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
from services.permissions.guards import (
    FILE_REWRITE_TOOLS,
    GUARD_KEY,
    GUARDED_TOOLS,
    changes_directory,
    guard,
    is_guarded,
)
from services.registry import ModelRegistry
from services.sandbox.thread_shells import ThreadShells
from services.vm.project import ProjectCopies
from tools.deps import RunDeps
from tools.files import unread_refusal
from tools.guarded import shell_cwd
from tools.home import resolve_run_home
from tools.run_home import home_of

logger = logging.getLogger(__name__)

#: Reviews of one batch in flight at once. Each is a utility-model round trip, so the cap
#: protects the far side (and a local runtime's queue); the point is overlapping latencies.
_REVIEW_CONCURRENCY = 4

_UNEXPLAINED = "This call always needs your approval."
_UNCHECKED = "This call could not be checked, so it needs your approval."


@dataclass(kw_only=True)
class GrantApproved(ToolApproved):
    """An approval a standing conversation grant produced, marked as one.

    The approve route re-validates what a parked turn settled without the operator, since a
    grant can be revoked or lapse while the run waits. Only a grant's approval has anything
    to re-validate: a review that cleared a call on its own grounds leaves no grant, so a
    re-check would deny a call the operator was never offered.

    A review can still produce one: at Auto a grant is an input, but where it supplied the
    authorization the allow rests on the same revocable thing (``ReviewOutcome.by_grant``),
    so it is marked and re-checked alike.

    The mark rides on the decision because that is the only part of the settled pile that
    crosses into the parked payload.

    ``needs_whole_tool`` is what the re-check must demand, deliberately not the width the
    operator held. Only an allow that cleared an act nobody can undo
    (``services/permissions/decide.py``) turns on the wider width. Recording the held width
    would break Manual and Edit, where either settles the call: a thread with both grants
    would measure "tool", and revoking only that one would deny a call the surviving
    command grant fully answers for.
    """

    #: Whether this allow rests on the wider width; False for all but one kind of allow.
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
    metadata: MutableMapping[str, dict[str, object]] | None = None,
) -> tuple[dict[str, ToolApproved | ToolDenied], list[ToolCallPart]]:
    """Rule on every call this hop deferred, returning ``(settled, manual)``.

    ``metadata`` is the deferral's own, by call id: a call marked guarded there goes to the
    operator, and every guarded call's reason is written into it.

    ``settled`` holds the decisions the turn continues on at once: an allow the model never
    notices, or a denial it is told in place of the result. ``manual`` is what is left for
    the operator; a non-empty one parks the turn.

    A guarded call is ruled first (``services/permissions/guards.py``): it goes to the
    operator at every level, and no grant or reviewer can answer it. A level that refuses
    outright still refuses.

    The level rules next; a grant may answer it but never overturn it. A standing grant
    settles a call the level would have put to the operator. But a refusing level already
    carries their answer: a grant recorded while the thread could act is not consent in one
    since set to act in nothing (Plan's contract is that nothing changes). The resume path
    reads it the same way (``routes/runs.py``). Grants are conversation-scoped, so a
    stateless turn always asks, and a command tool's grant is scoped to the command's
    leading words: a yes to `uv run pytest` settles the next test run and nothing else.

    At the top level grants are not consulted: nothing is asked, so there is no prompt to
    skip and no park for a revocation to catch. The approval is the level's, recorded as
    such (:func:`_by_level`).

    At Auto a grant is an input to the review, not the answer. A grant naming one command
    rides along as the authorization for that act and the review still runs. The wider grant
    (this tool and everything it runs) is read as the answer, being the operator's reply to
    the review's own question and the only thing that also clears an act nobody can undo.
    Neither bypasses the judge or stands in for a review that couldn't run, which parks
    whatever the grants say (:func:`services.permissions.review`). An allow the grant's
    authorization produced is recorded as the grant's (:class:`GrantApproved`) so the resume
    re-checks it if the operator revokes while the rest of the batch waits.

    ``turn_start`` and ``budget`` belong to the turn, threaded from ``drive_turn``: where
    its own messages begin (so the reviewer sees the opening request however many tools ran
    since) and the ceiling on model reviews per turn.
    """
    granted: list[GrantInfo] = []
    store = caps.get_optional(ApprovalGrantStore)
    if store is not None and conversation_id is not None:
        granted = await store.list(run.owner_id, conversation_id)
    # Ruled before any review, so the batch knows which calls need a model before paying to
    # resolve one. Each row carries the decision and whether a grant produced it: an ALLOW
    # can come from a grant or an allowing level, and only the first is revocable.
    home = await _guard_home(deps, approvals)
    # A write the read ledger would turn away is neither asked nor reviewed; the model is
    # told to read the file first.
    settled: dict[str, ToolApproved | ToolDenied] = {}
    asking: list[ToolCallPart] = []
    for call in approvals:
        refusal = (
            await unread_refusal(deps, call.tool_name, call.args_as_dict(), home)
            if call.tool_name in FILE_REWRITE_TOOLS
            else None
        )
        if refusal is None:
            asking.append(call)
        else:
            settled[call.tool_call_id] = ToolDenied(message=refusal)
    guarded = _guard_reasons(asking, home, shell_cwd(deps), metadata)
    ruled = [
        (call, Decision.ASK, False)
        if call.tool_call_id in guarded and decide(permission, call.tool_name) is not Decision.BLOCK
        else (call, *_by_level(permission, call, granted))
        for call in asking
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
    manual: list[ToolCallPart] = []
    for call, decision, grant_backed in ruled:
        outcome = reviewed.get(call.tool_call_id)
        if outcome is not None:
            decision = outcome.decision
        if decision is Decision.ALLOW:
            # Reviewed, grant-backed, or allowed by a level: only the grant is worth
            # re-checking. A review leaning on a grant counts as the grant's (revocable). A
            # level's own allow must not be marked so: the resume would find no covering
            # grant and deny something no operator refused.
            by_grant = outcome.by_grant if outcome is not None else grant_backed
            settled[call.tool_call_id] = (
                GrantApproved(
                    # With no ``outcome`` a grant settled the call at a level that takes
                    # either width; only a review can require the wider one.
                    needs_whole_tool=outcome is not None and outcome.needs_whole_tool
                )
                if by_grant
                else ToolApproved()
            )
        elif decision is Decision.BLOCK:
            # Only a level refuses outright; a review's refusals became parks, since an act
            # nobody can undo is the one the operator most needs to see.
            settled[call.tool_call_id] = ToolDenied(
                message=blocked_message(permission, call.tool_name)
            )
        else:
            manual.append(call)
    return settled, manual


async def _guard_home(deps: RunDeps, approvals: Sequence[ToolCallPart]) -> RunHome | None:
    """The home the guard places paths against, opened only if a call has a guard rule."""
    if not any(call.tool_name in GUARDED_TOOLS for call in approvals):
        return None
    return await home_of(deps)


def _guard_reasons(
    approvals: Sequence[ToolCallPart],
    home: RunHome | None,
    cwd: str | None,
    metadata: MutableMapping[str, dict[str, object]] | None,
) -> dict[str, str]:
    """Why each call that must reach the operator at every level has to, by call id.

    Ahead of grants, level and reviewer, none of which may answer these. A call already
    marked guarded in ``metadata`` stays guarded, and every reason is written back so the
    park announces it and a restored park keeps it.
    """
    settings = get_settings()
    reasons: dict[str, str] = {}
    # Command tools run in order, so a `cd` moves where later calls start; after one,
    # relative paths can't be placed.
    cwd_known = True
    for call in approvals:
        marked = metadata.get(call.tool_call_id) if metadata is not None else None
        args = call.args_as_dict()
        if is_guarded(marked):
            reason = marked.get(GUARD_KEY) if marked is not None else None
            reasons[call.tool_call_id] = reason if isinstance(reason, str) else _UNEXPLAINED
        elif call.tool_name in GUARDED_TOOLS:
            try:
                found = guard(
                    call.tool_name, args, home=home, settings=settings, cwd=cwd, cwd_known=cwd_known
                )
            except Exception:  # a guard that raises would abort the operator's turn
                logger.warning("guard failed on %s", call.tool_name, exc_info=True)
                found = _UNCHECKED
            if found is not None:
                reasons[call.tool_call_id] = found
        if cwd_known and changes_directory(call.tool_name, args):
            cwd_known = False
    if metadata is not None:
        for call_id, reason in reasons.items():
            metadata.setdefault(call_id, {})[GUARD_KEY] = reason
    return reasons


def _by_level(
    permission: str, call: ToolCallPart, granted: Sequence[GrantInfo]
) -> tuple[Decision, bool]:
    """What the thread's level says about one call, letting standing grants answer only
    where the level was asking a question they can answer.

    Returns the decision and whether a grant produced it, which can't be recovered from the
    decision once several things return ``ALLOW``.

    Three answers stand whatever the grants say. A refusal already carries the operator's
    answer in the level they chose. A review is the level doing the deciding; a grant
    short-circuiting it would turn one "allow for this conversation" into a thread where no
    command is ever looked at again, so the grant goes to the review instead. An allow has
    nothing for a grant to add (nothing is asked at the top level); recording it as a
    grant's would make the resume deny a call that never rested on one.

    Takes the call, not just its name: a command tool's grant is scoped to the command
    (``services/approval_grants.py``), so what settles a call at Manual and Edit is a yes
    to this act, not everything the tool could do.
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

    ``home`` is the one the caller already opened for the batch.

    The reviewer (a registry lookup and model construction) and the transcript (one walk of
    recent history) are built once and shared across the batch.

    The fence token is minted once too, which is what makes sharing pay on the server: with
    a nonce per prompt each diverged from its siblings within a few tokens. One nonce per
    batch with the shared prose first (``reviewer.review_prompt``) lets later reviews arrive
    on a prefix the engine already processed. Per batch and no wider: fresh every turn,
    never reused across threads or over time.
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
    # A non-host command is held to its reach only by running in the VM, and the shell
    # refuses every command while it is down.
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
    """The home every path in this batch is measured against, from where the thread's shell stands.

    Resolved, not read off the run's memo: a turn's first shell command is deferred before
    any tool has opened a workspace, so the memo is empty. Judging against None is strictest
    (every absolute or upward path escapes) but wrong for that call, escalating ordinary
    work a resolved root clears.

    Once per batch, and only if a call is placed against a root
    (:func:`measured_against_root`): opening a workspace is a `git worktree add` or sandbox
    start, which a batch of mail sends shouldn't pay for.

    A failure never ends the turn: the tool will report the unopenable workspace in words
    the model can act on, and meanwhile the judge answers strictly with no root.
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
    # A working directory outside the home is where the shell won't start
    # (`HomeShell.stand_within`).
    return home.fence(cwd if cwd is not None and home.place_of(cwd) is not None else None)


async def resolve_reviewer(caps: ServiceContainer, owner_id: str) -> Reviewer | None:
    """The Auto reviewer for this run, or None when there is nothing to review with.

    Resolves the utility role through ``resolve_background`` as titling, verification and
    delegation do, so it is cheap by construction and works with only ``main`` bound.

    None parks at every caller: a review that can't run is not one that passes.
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
        # Reasoning off as for the namer and judge: background work inside a watched turn.
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

    ``root`` is the run's home, resolved once per batch (:func:`_judged_root`); None is the
    strictest reading, where every absolute or upward path escalates. ``fenced`` is the host
    fact the structural stage can't look up, ``granted`` how far the standing grant reaches
    (nothing, the one act, or the whole tool), and ``nonce`` the batch's shared fence token
    so every prompt opens on the same bytes.
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
            # `review` never returns REVIEW, so the map is total; a lookup so a fourth
            # Decision member fails here, not on a client.
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


#: Review outcomes on the wire. `REVIEW` is absent: it is the question, and returning it
#: would be a bug. `BLOCK` is no longer returned (an unrecoverable act parks) but stays
#: because stored events of earlier threads still carry it and clients render those.
WIRE_DECISION: dict[Decision, Literal["allow", "ask", "block"]] = {
    Decision.ALLOW: "allow",
    Decision.ASK: "ask",
    Decision.BLOCK: "block",
}
