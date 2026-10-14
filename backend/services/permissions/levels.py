"""How much rope the model gets — two knobs, and five names over them.

A permission level is the second axis of a thread. The workspace says whose files it works
in; the level says how far the model may go without stopping to ask. They are orthogonal
on purpose: every workspace carries all five levels, and a level means the same thing in
each.

**Why two knobs rather than a branch per name.** The obvious shape is named levels and a
decision function that switches on the name — which works until one more combination is
wanted, at which point every switch has to grow a case and the ones that are missed fail
silently. So a level is stored and shown as a name, and *resolved* to a pair:

- a **ceiling** on :class:`~services.tool_sensitivity.Sensitivity` — how far the model may
  reach without permission at all. ``read`` permits observation only; ``workspace_write``
  also permits changing what this installation owns; the top class permits every kind of
  reach there is, which is what the room for a third was for;
- an **approval policy** — what happens to an act that reaches past that ceiling: withhold
  the tool outright, park for the operator, send it to review, or allow it outright.

The levels are presets over that pair. A sixth is a row here, and nothing else moves —
which is how the fifth arrived: ``yolo`` is one entry in :data:`PERMISSIONS`, one member
of :class:`ApprovalPolicy`, and one branch in ``decide.py`` for the gate it deliberately
removes (below). Nothing switched on a name, so nothing else had to be found and edited.

The ceiling is a sensitivity class rather than a name of its own because that is what
makes the pair decidable: the classes already say what a tool *does*, so "may this run
without asking?" is a comparison rather than a list of tool names to keep in step.

**A level does not take a gate away — with one level as the stated exception.** Levels
widen; they do not narrow. A tool that gates itself — the global-recall pause, a skill
edit — is still answered at every level below the top one, because the reason it gates is
not the one this axis reasons about (a recall pulls untrusted content into the model's
context; it changes nothing the ceiling describes). What clears a tool's own gate is the
operator: at the prompt, through a standing conversation grant, or — at the level whose
meaning is that they asked for their answers to be given for them — through the review.
That asymmetry is what keeps adding a level from quietly deleting a protection nobody
re-examined.

**Yolo is where the operator deletes it on purpose, and it is the only one.** Its whole
proposition is that nothing in a thread stops, and a "free rein" level that still parked
on a corpus recall or a vault read would be a level whose name lied about it. So its
policy answers *both* questions with the same word — the level's, and the tool's own —
and it is the sole reason :class:`ApprovalPolicy` has a fourth member rather than the
three the other knobs produce. The exception is one row and one branch, both of which say
so; what makes it defensible is that it is reachable only by an operator naming it, never
by a default, a fallback or a degrade (:data:`DEFAULT_PERMISSION`,
:data:`STRICTEST_PERMISSION`), and that it removes the *asking* and not the fence — a
declared reach still builds the sandbox a command runs inside, and one that needs more
than it declared still fails in there rather than going ahead.

**At Auto, "through the review" includes the review's own deterministic answer**, and for
a self-gated *read* that answer costs no model call: ``judge.py`` clears a tool the
shipped catalog classifies as observing on the strength of the class, at the tier it
records as ``read``. The gate is still answered — by the stage the operator's chosen level
delegates to — but nothing weighs the arguments, because for that class of tool there is
nothing in them to weigh. It is the one place where a level's delegation resolves without
a model, and the review row says so in those words rather than reporting it as a review.

Two sets are exempt in the other direction, here rather than at a call site because both
halves of the enforcement need them. The model's own task list and the tools that move a
thread in and out of plan mode are reachable at every level (:data:`PLANNING_TOOLS`), since
a read-only turn whose only possible ending is a submitted plan cannot be made to ask
permission to reach the tool that submits one. And messaging within a delegation tree is
reachable at every level (:data:`COORDINATION_TOOLS`), since a read-only agent sent to look
into something has to be able to say what it found before it finishes; the tool gates
itself on where the message goes.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

from prompts.levels import AUTO_LEVEL, MANUAL_LEVEL, PLAN_LEVEL, YOLO_LEVEL
from services.tool_sensitivity import (
    EXTERNAL_PREFIX,
    TOP_SENSITIVITY,
    Sensitivity,
    classified,
    sensitivity_of,
    tools_above,
)

#: The stored vocabulary. A plain string on the conversation row, like the workspace kind: a
#: restored backup or a row written by another build must still load.
type PermissionLevel = Literal["plan", "manual", "edit", "auto", "yolo"]

#: The five **in the order they are offered**, strictest first. Presentation order, not a
#: comparison: ``edit`` and ``auto`` share a ceiling and differ only in who answers at it,
#: so nothing should read a ``>`` into that step. A tuple rather than the set below
#: because a set has no order to offer — a picker built from one lists the levels
#: differently on each boot, string hashing being randomised per process.
#:
#: ``yolo`` is last because it permits the most, and being last is the whole of what the
#: picker owes it: it is offered in the same list as the rest rather than tucked behind a
#: setting, since an operator who cannot find the level they want reaches for the nearest
#: thing instead, and the nearest thing to "stop asking me" is a standing grant on a tool
#: that runs commands. The copy beside it, not its position, is what says what it costs.
PERMISSION_LADDER: tuple[PermissionLevel, ...] = ("plan", "manual", "edit", "auto", "yolo")

#: The five, as a set to validate against. Derived from the ladder so no caller re-lists
#: them and the two cannot disagree.
PERMISSION_LEVELS: frozenset[str] = frozenset(PERMISSION_LADDER)

#: What a fresh thread starts at, and what a caller with no level to pass gets.
#:
#: ``auto`` reaches no further than ``edit`` — the two share a ceiling — and differs only
#: in who answers at that boundary: Edit stops and asks, Auto rules first and asks about
#: what it will not settle. The default moved once the ruling became structural: contained
#: offline work is cleared by the grammar and an OS fence rather than by a model's opinion
#: of a program name, so the ordinary work of a thread stopped costing the operator a
#: prompt while everything that leaves the workspace still reaches them.
#:
#: **This is the default for a *fresh* thread only.** Existing rows keep the level they
#: were stored with (``models/conversation.py`` changed its Python default and not its
#: column default, so no migration re-points a thread the operator already set).
DEFAULT_PERMISSION: PermissionLevel = "auto"

#: The level that does the least — where an unreadable stored value lands.
STRICTEST_PERMISSION: PermissionLevel = "plan"


class ApprovalPolicy(StrEnum):
    """What becomes of an act that reaches past the level's ceiling."""

    #: Never offered. The tool is dropped from the catalog before the model sees it, so
    #: there is nothing to ask about — the only form of read-only that survives a model
    #: that decides otherwise (``services/tool_policy.py``).
    WITHHOLD = "withhold"
    #: Park and wait for the operator's decision.
    ASK = "ask"
    #: Judged, then reviewed; parks on doubt.
    REVIEW = "review"
    #: Run it, asking nobody. The one policy that also answers a gate the *tool* raised
    #: for reasons this axis knows nothing about — see the module docstring for why
    #: exactly one level may do that, and ``decide.py`` for the single branch where it
    #: happens. Like every member here it rules on the call in hand and nothing else;
    #: what makes the thread feel unstopped is that it gives this answer every time.
    ALLOW = "allow"


@dataclass(frozen=True)
class PermissionSpec:
    """One named level, as the pair it resolves to."""

    #: The stored value, and the identity — ``PERMISSIONS[spec.level] is spec``.
    level: PermissionLevel
    #: The highest sensitivity class that passes unasked. ``read`` reaches nothing;
    #: ``workspace_write`` also reaches the run's own files, its plan and this
    #: installation's records — state we own, can show the operator, and can undo.
    ceiling: Sensitivity
    #: What happens when a tool reaches further.
    approval_policy: ApprovalPolicy
    #: The prompt fragment this level adds, or "" when it adds nothing — mirroring
    #: ``ModeSpec.instructions``, and for the same reason: a level's prose belongs with the
    #: rest of that level's declaration rather than in a branch at the engine. One of the
    #: four says nothing (:mod:`prompts.levels` explains which and why), so the registration
    #: is unconditional and a thread pays only for the level it is at.
    instructions: str = ""


PERMISSIONS: Mapping[PermissionLevel, PermissionSpec] = {
    # Read-only, and enforced by absence rather than by asking: a Plan turn is offered no
    # mutating tool at all, so it ends the only way it can — in a plan the operator reads
    # and accepts. Asking instead would make the read-only promise depend on the model
    # agreeing to it, which is the one moment it stops holding.
    "plan": PermissionSpec(
        level="plan",
        ceiling=Sensitivity.READ,
        approval_policy=ApprovalPolicy.WITHHOLD,
        instructions=PLAN_LEVEL,
    ),
    # Read-only until told otherwise, one act at a time. The tools stay in the catalog —
    # the model must be able to propose the thing it needs permission for.
    "manual": PermissionSpec(
        level="manual",
        ceiling=Sensitivity.READ,
        approval_policy=ApprovalPolicy.ASK,
        instructions=MANUAL_LEVEL,
    ),
    # Change the workspace freely, stop at its edge and ask. Running a program, reaching a
    # mail or calendar server, driving the operator's own browser session and reading a
    # credential all sit past that edge.
    "edit": PermissionSpec(
        level="edit", ceiling=Sensitivity.WORKSPACE_WRITE, approval_policy=ApprovalPolicy.ASK
    ),
    # The default, and the same reach as Edit: the operator's decision is replaced by a
    # review rather than removed — a structural judge over the command's own syntax, then
    # a model reviewer, parking on any doubt and on every way the review can fail
    # (`decide.py`). It says a line about itself because it asks the model to *declare*
    # something (`prompts/levels.py`), which is the one thing here a model has to be told.
    "auto": PermissionSpec(
        level="auto",
        ceiling=Sensitivity.WORKSPACE_WRITE,
        approval_policy=ApprovalPolicy.REVIEW,
        instructions=AUTO_LEVEL,
    ),
    # Free rein: the operator has decided to stop being asked, and this is the level that
    # says so in one word instead of making them assemble it out of standing grants.
    #
    # The ceiling is the top class rather than one of the three named ones, because what
    # this level permits is *every kind of reach* and the three are tied
    # (`tool_sensitivity.TOP_SENSITIVITY`) — so nothing is ever beyond scope here, and the
    # toolset marks nothing for approval in the first place. The policy is what settles
    # the rest: a tool that gates itself is cleared too, which is the one place a level
    # takes a gate away and the reason this member exists.
    #
    # What it does *not* remove is the fence. A command still declares its reach and still
    # runs inside the sandbox profile built from that declaration, so a command that needs
    # more than it declared fails in there exactly as it does at Auto. This level removes
    # the asking; the containment is not the asking's to remove.
    "yolo": PermissionSpec(
        level="yolo",
        ceiling=TOP_SENSITIVITY,
        approval_policy=ApprovalPolicy.ALLOW,
        instructions=YOLO_LEVEL,
    ),
}


#: How permissive each policy is, once the ceiling has been compared. Withholding the tool
#: is the least a level can do with an act it does not permit; parking asks the operator;
#: reviewing answers for them; allowing asks nobody. Only meaningful *within* one ceiling
#: — a level that reaches further is more permissive whatever it does at the boundary.
_POLICY_PERMISSIVENESS: Mapping[ApprovalPolicy, int] = {
    ApprovalPolicy.WITHHOLD: 0,
    ApprovalPolicy.ASK: 1,
    ApprovalPolicy.REVIEW: 2,
    ApprovalPolicy.ALLOW: 3,
}

#: The levels that can change something without being asked first — derived from the pair
#: rather than named, so a fifth preset is a row in :data:`PERMISSIONS` and nothing else
#: moves. A ``read`` ceiling is the definition of a level that cannot act: it reaches
#: nothing but observation, so raising a thread *to* it would be a no-op with extra steps.
ACTING_PERMISSIONS: frozenset[PermissionLevel] = frozenset(
    level for level, spec in PERMISSIONS.items() if spec.ceiling is not Sensitivity.READ
)


def permission_level(level: str) -> PermissionLevel:
    """A stored permission value, falling back to the strictest level rather than the
    default one.

    The two axes of a thread degrade in the same direction and for the same reason — a
    value that reaches this comes off a database row or a parked run's payload, both of
    which outlive a rename — but the conservative answer differs. An unknown *workspace
    kind* is a sandbox because a sandbox reaches the least; an unknown *level* is Plan
    because Plan does the least. A corrupt value leaves the model able to read and to
    plan, and unable to act, which is a failure the operator can see and correct rather
    than one that quietly grants.
    """
    for known in PERMISSION_LADDER:
        if level == known:
            return known
    return STRICTEST_PERMISSION


def permission_spec(level: str) -> PermissionSpec:
    """The spec for a stored level value. Never raises — see :func:`permission_level`."""
    return PERMISSIONS[permission_level(level)]


def _permissiveness(spec: PermissionSpec) -> tuple[int, int]:
    """How far a level goes, as a comparable pair: how far it reaches unasked, then what it
    does with an act that reaches further. Derived from the two knobs for the same reason
    everything else here is — a fifth preset orders itself instead of being slotted into a
    hand-written list somebody has to remember to edit."""
    return (spec.ceiling.escalation, _POLICY_PERMISSIVENESS[spec.approval_policy])


def stricter_permission(a: str, b: str) -> PermissionLevel:
    """Whichever of ``a`` and ``b`` permits the less.

    For the caller that opens a thread *from inside another one*: the spawned thread must
    never be able to do more than the thread that spawned it, whatever level a fresh
    thread would start at. Taking the stricter of the two is what stops one
    approved spawn from buying a standing level the operator never chose. A tie resolves
    to ``a``, since neither permits more than the other."""
    return min(
        (permission_level(a), permission_level(b)),
        key=lambda level: _permissiveness(PERMISSIONS[level]),
    )


# The two planning surfaces — permitted at every level, whatever their ceiling says.
#
# The **task list** is the model's own scratchpad: a level that asks before *acting* has no
# business interrupting it, and a read-only turn that could not record what it had worked
# out would have no way to finish. The **plan tools** are how a Plan-level turn ends at all:
# `plan_enter` only ever narrows what the thread may do, `plan_read` reaches nothing, and
# `plan_submit` — the one that raises the level — is held by its own `requires_approval`
# marking rather than by this gate, so the operator still decides. Withholding it here
# instead would leave Plan with no exit and the exemption is what makes the mode work.
#
# The whole surviving surface is listed rather than the writes alone, so the exemption reads
# as "the two planning surfaces" and a reader does not have to work out which half of it
# needed naming. The names are literals for the reason every other tool-name set in
# `services/` is (`tools/` sits above it in the dependency order), and
# `tests/test_tool_sensitivity.py` pins them against the live catalog.
PLANNING_TOOLS = frozenset(
    {
        "plan_enter",
        "plan_read",
        "plan_submit",
        "tasks_read",
        "tasks_update_statuses",
        "tasks_write",
    }
)


# Messaging within a delegation tree — permitted at every level. Its class stays where
# directing another agent belongs, and the tool raises its own approval for a message that
# reaches an agent able to write from anywhere but below it (`tools/delegation.py`), so the
# exemption only spares a read-only agent from being unable to report early or to reach a
# read-only sibling.
COORDINATION_TOOLS = frozenset({"agents_message"})

#: Every tool the level gate never elevates.
LEVEL_EXEMPT_TOOLS = PLANNING_TOOLS | COORDINATION_TOOLS


def beyond_scope(level: str, tool: str, *, declared: Sensitivity | None = None) -> bool:
    """Whether ``tool`` reaches past what ``level`` permits unasked.

    ``declared`` is the class the toolset stated about the tool itself
    (:func:`~services.tool_sensitivity.declared_sensitivity`). It wins over both the name
    registry and the fallback below, because it is knowledge where they are inference —
    and it is the only answer available for a toolset this installation composes at run
    time rather than ships in the catalog.

    The one question both halves of the enforcement ask, so they cannot disagree about
    where the line is: the toolset marks every tool past it as needing approval, and the
    decision point that sees the resulting call reads the same answer to know that the
    level — rather than the tool's own marking — is what deferred it.

    **A tool this installation cannot classify is elevated only by a level that permits
    nothing.** The unclassifiable names are the operator's own MCP and connector tools —
    every tool in the catalog proper carries a class, and ``tests/test_tool_sensitivity.py``
    fails in *both* directions if one stops doing so — and those already carry a per-tool
    decision of the operator's: the trust list (``services/external_tools.py``), which
    pauses an untrusted one from inside the call and lets a trusted one through.
    Manufacturing a second gate on top of a guess would replace that explicit answer with
    an inferred one, and would make "trusted" mean nothing at the two levels where the
    operator uses it. Where the level's promise is that the thread changes *nothing*, the
    guess is load-bearing and the promise wins — there is no other way to keep it against
    a tool nothing here can bound.

    **Everything else unclassified fails closed**, and the split between the two is the
    prefix the external surface already carries. Deferring to the trust list is an answer
    only where there *is* one; for any other unknown name — a tool added to the catalog
    without a class, a rename that outran the registry — there is nothing to defer to, and
    treating "we have never heard of this" as "it may act unasked" is the one reading that
    cannot be justified. So it takes :func:`~services.tool_sensitivity.sensitivity_of`'s
    own conservative fallback and reaches
    :func:`~services.permissions.decide.decide` like anything else past the ceiling.

    In a correct build that branch is unreachable — ``tests/test_tool_sensitivity.py``
    fails in both directions if a catalog tool loses its class — which is exactly why it
    is written the safe way: the cost of it never firing is nothing, and the cost of it
    firing open is a tool acting unasked at the levels that let the model act at all.

    **At the top level all of that resolves to "no" and none of it is wasted.** A ceiling
    of :data:`~services.tool_sensitivity.TOP_SENSITIVITY` is above nothing, so an
    unclassified name, a fail-closed fallback and a declared class all compare the same
    way and this returns False — which is the correct answer there and reached through the
    same comparison as every other, rather than through a name check that would have to be
    kept in step with the registry. The conservatism above is not bypassed at that level;
    it is applied, and finds nothing left to elevate.
    """
    if tool in LEVEL_EXEMPT_TOOLS:
        return False
    spec = permission_spec(level)
    if declared is not None:
        return declared.above(spec.ceiling)
    if not classified(tool) and tool.startswith(EXTERNAL_PREFIX):
        return spec.ceiling is Sensitivity.READ
    return sensitivity_of(tool).above(spec.ceiling)


def tools_beyond_scope(level: str) -> frozenset[str]:
    """Every *classified* tool ``level`` does not permit — the set form of
    :func:`beyond_scope`, for the caller that narrows a catalog rather than ruling on one
    call. Unclassified names are absent by construction and do not need to be there: only
    a call can reveal one, and the decision point that sees it resolves it the same way.
    """
    return tools_above(permission_spec(level).ceiling) - LEVEL_EXEMPT_TOOLS
