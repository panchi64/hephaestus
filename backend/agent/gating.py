"""Ruling on the calls a turn deferred: the engine's half of the decision.

A turn wanting a sensitive tool ends with the call unexecuted; :func:`settle_deferred`
walks the batch and returns the calls settled without a human and those needing one, so
``turn.py`` keeps control flow, not policy. ``services/permissions`` owns the rules; this
module reads the run's facts they need (grants, the home, the VM) and records which
producer an allow came from, since the resume and the executing tool re-check some of them.
"""

from __future__ import annotations

import logging
from collections.abc import MutableMapping, Sequence
from dataclasses import dataclass

from pydantic_ai import ToolApproved, ToolDenied
from pydantic_ai.messages import ToolCallPart

from core.config import get_settings
from core.container import ServiceContainer
from runs import Run
from services.approval_grants import ApprovalGrantStore, GrantInfo, covered_by_grant
from services.home.table import RunHome
from services.permissions import (
    AUTO_KEY,
    EGRESS_TOOL,
    AutoOutcome,
    Decision,
    auto_decision,
    blocked_message,
    decide,
)
from services.permissions.guards import (
    GUARD_KEY,
    GUARDED_TOOLS,
    changes_directory,
    guard,
    is_guarded,
)
from tools.deps import RunDeps
from tools.guarded import shell_cwd
from tools.home_workspace import auto_env
from tools.run_home import home_of

logger = logging.getLogger(__name__)

_UNEXPLAINED = "This call always needs your approval."
_UNCHECKED = "This call could not be checked, so it needs your approval."


@dataclass(kw_only=True)
class GrantApproved(ToolApproved):
    """An approval a standing conversation grant produced, marked as one.

    The approve route re-validates what a parked turn settled without the operator, since a
    grant can be revoked or lapse while the run waits. The mark rides on the decision
    because that is the only part of the settled pile that crosses into the parked payload.
    """


@dataclass(kw_only=True)
class EnvApproved(ToolApproved):
    """An approval Auto gave because the conversation's VM bounded the call, marked as one.

    The VM can gain hosts while the run waits (an egress grant), so the resume re-checks
    it, as the executing tool does for a call that runs at once (``RunDeps.env_cleared``).
    """


async def settle_deferred(
    run: Run,
    approvals: Sequence[ToolCallPart],
    *,
    caps: ServiceContainer,
    conversation_id: str | None,
    deps: RunDeps,
    permission: str,
    metadata: MutableMapping[str, dict[str, object]] | None = None,
) -> tuple[dict[str, ToolApproved | ToolDenied], list[ToolCallPart]]:
    """Rule on every call this hop deferred, returning ``(settled, manual)``.

    ``settled`` holds the decisions the turn continues on at once: an allow the model never
    notices, or a denial it is told in place of the result. ``manual`` is what is left for
    the operator; a non-empty one parks the turn. ``metadata`` is the deferral's own, by
    call id: a guarded call's reason and a parked Auto call's reason are written into it so
    the park announces them.

    In order:

    1. A guarded call (``services/permissions/guards.py``) goes to the operator at every
       level, and no grant or rule can answer it, unless the level refuses it outright.
    2. The level rules. A refusal stands whatever the grants say; a level's own allow (Yolo)
       needs no grant.
    3. Where the level asks, a standing grant covering the call settles it. Grants are
       conversation-scoped, so a stateless turn always asks.
    4. At Auto, :func:`services.permissions.auto_decision` settles the call: what the
       VM bounds runs, then what a grant covers, and the rest parks.

    An allow a grant produced is recorded as :class:`GrantApproved` and one the VM
    produced as :class:`EnvApproved`, so the resume re-checks them.
    """
    granted: list[GrantInfo] = []
    store = caps.get_optional(ApprovalGrantStore)
    if store is not None and conversation_id is not None:
        granted = await store.list(run.owner_id, conversation_id)
    home = await _guard_home(deps, approvals)
    guarded = _guard_reasons(approvals, home, shell_cwd(deps), metadata)
    ruled = [
        (call, Decision.ASK, False)
        if call.tool_call_id in guarded and decide(permission, call.tool_name) is not Decision.BLOCK
        else (call, *_by_level(permission, call, granted))
        for call in approvals
    ]
    at_auto = [call for call, decision, _ in ruled if decision is Decision.REVIEW]
    auto = await auto_batch(deps, at_auto, granted) if at_auto else {}
    settled: dict[str, ToolApproved | ToolDenied] = {}
    manual: list[ToolCallPart] = []
    for call, decision, grant_backed in ruled:
        outcome = auto.get(call.tool_call_id)
        if outcome is not None:
            decision = outcome.decision
        if decision is Decision.ALLOW:
            settled[call.tool_call_id] = _approval(call, outcome, grant_backed, deps)
        elif decision is Decision.BLOCK:
            settled[call.tool_call_id] = ToolDenied(
                message=blocked_message(permission, call.tool_name)
            )
        else:
            if outcome is not None and metadata is not None:
                metadata.setdefault(call.tool_call_id, {})[AUTO_KEY] = outcome.reason
            manual.append(call)
    return settled, manual


def _approval(
    call: ToolCallPart, outcome: AutoOutcome | None, grant_backed: bool, deps: RunDeps
) -> ToolApproved:
    """The allow for one call, marked with what produced it. A level's own allow is plain:
    the resume would find no grant behind it and deny something no operator refused."""
    if outcome is None:
        return GrantApproved() if grant_backed else ToolApproved()
    if outcome.by_grant:
        return GrantApproved()
    if outcome.by_environment:
        deps.env_cleared.add(call.tool_call_id)
        return EnvApproved()
    return ToolApproved()


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

    Ahead of grants, level and Auto, none of which may answer these. A call already marked
    guarded in ``metadata`` stays guarded, and every reason is written back so the park
    announces it and a restored park keeps it.
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
    where the level was asking. Returns the decision and whether a grant produced it, which
    can't be recovered from the decision once several things return ``ALLOW``.

    A refusal, an allow and Auto's ``REVIEW`` stand as they are: Auto reads the grants
    itself, after the environment (:func:`auto_batch`).
    """
    decision = decide(permission, call.tool_name)
    if decision in (Decision.BLOCK, Decision.REVIEW, Decision.ALLOW):
        return decision, False
    if covered_by_grant(call.tool_name, call.args_as_dict(), granted):
        return Decision.ALLOW, True
    return decision, False


async def auto_batch(
    deps: RunDeps, calls: Sequence[ToolCallPart], grants: Sequence[GrantInfo]
) -> dict[str, AutoOutcome]:
    """Auto's answer for every call the level sent to it, by ``tool_call_id``. The VM facts
    are read once for the batch, and an egress request anywhere in it counts as network for
    all of it, since it would open the network before the commands run."""
    env = await auto_env(
        deps, egress_requested=any(call.tool_name == EGRESS_TOOL for call in calls)
    )
    return {
        call.tool_call_id: auto_decision(
            call.tool_name,
            env=env,
            granted=covered_by_grant(call.tool_name, call.args_as_dict(), grants),
        )
        for call in calls
    }
