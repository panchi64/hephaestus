"""Plan mode's three tools — enter it, read the plan, submit one for approval.

The counterpart to ``tools/tasks.py``. That one is the running checklist the agent keeps
for itself at every level; this is the written document a Plan-level turn produces for the
operator to say yes to. They were one word and one feature once, which is why neither
module now uses the other's.

**The level moves from inside the run, and takes effect inside the turn.** Both
``plan_enter`` and an approved ``plan_submit`` change what the thread may do, and both do it
by writing the conversation row *and* by editing ``ctx.deps`` in place. The second half is
what makes it real immediately: ``RunDeps`` is a plain dataclass, and both gates in
``tools/toolsets.py`` re-read it on every model request, so the narrowed — or widened —
catalog lands on the very next request rather than next turn. Without it, ``plan_enter``
would be an announcement the model could ignore for the rest of the turn, and an approved
plan would be an agreement the model had no tools to carry out.

**``plan_submit`` raises ``ApprovalRequired`` from inside the call** rather than carrying
``requires_approval=True``, and the difference is load-bearing. The static marking defers
the call *before* the body runs, and the body is what records the plan — so the panel would
have nothing to render while the operator decided, and a reload mid-decision would find no
document. Raising from inside means the first invocation writes and announces the plan and
*then* parks; the re-invocation after an approval (``ctx.tool_call_approved``) is the one
that accepts it. The name is declared in ``GATED_TOOLS`` for the same reason every other
tool that gates this way declares itself: a name missing from that union is missing from the
operator's approval-scope vocabulary.

**All three are exempt from the level gate** (``services/permissions.PLANNING_TOOLS``).
``plan_enter`` only ever narrows, ``plan_read`` reaches nothing, and ``plan_submit`` is held
by its own approval rather than by the level — which it has to be, since the level it would
be held by is the one it exists to end.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field
from pydantic_ai import FunctionToolset, RunContext
from pydantic_ai.exceptions import ApprovalRequired

from runs.lanes import is_attended
from services.permissions import stricter_permission
from services.plan_mode import ACTING_LEVEL, PLAN_SUBMIT_TOOL, PLANNING_LEVEL, PlanMode

from .deps import RunDeps

#: The conditionally-gated name this category contributes — it raises ``ApprovalRequired``
#: from inside the call, which no amount of inspection can discover.
GATED_TOOLS: frozenset[str] = frozenset({PLAN_SUBMIT_TOOL})

#: What a tool here returns when plan mode is not wired (a stateless run, or a turn with no
#: conversation to key on). Plan mode is a property of a thread; a run without one cannot
#: enter it, and saying so plainly is better than a park nobody can answer.
NO_THREAD = (
    "Plan mode is not available in this run — it belongs to a conversation, and this "
    "one has none. Carry on and decide with your best judgment."
)

#: What a turn that tries to plan in a run nobody is watching gets back instead of
#: stranding itself. Belt-and-braces, exactly as ``tools/builtin.py`` is for ``ask_user``:
#: ``services/tool_policy.ATTENDED_ONLY_TOOLS`` withholds both tools from those runs, so
#: this should be unreachable — but "unreachable" and "hangs the run until the process
#: restarts" are too far apart to leave to one gate. Worse here than for a question, in
#: fact: ``plan_enter`` also takes away every tool the run could have finished with.
NO_OPERATOR = (
    "No operator is available to approve a plan in this run (it is running unattended). "
    "Carry the work out directly, and say what you assumed."
)

#: What a submission that could not be stored gets back instead of parking on it. The
#: operator would be asked to approve a document the panel cannot show them — the store is
#: also what the panel reads on a reload — so there is nothing to decide about.
NO_RECORD = (
    "This plan could not be stored, so it cannot be put to the operator (the vault is "
    "locked). Ask them to unlock it, then submit again."
)


def _unattended(ctx: RunContext[RunDeps]) -> bool:
    """Whether this run has nobody in front of it to answer a plan. See :data:`NO_OPERATOR`."""
    return not is_attended(ctx.deps.run.kind)


def _retarget(ctx: RunContext[RunDeps], level: str) -> None:
    """Point this run's deps at ``level``, so the change binds for the rest of the turn.

    The conversation row is the durable half and the next turn reads it; this is the half
    that matters *now*: both gates in ``tools/toolsets.py`` re-read ``deps`` on every model
    request, so the narrowed — or widened — catalog lands on the next one.

    **One assignment, and `disabled_tools` is not touched.** It used to rebuild that set by
    subtracting the old level's withheld names and adding the new level's, which was wrong
    in a way nothing visible caught: the set is a *union* of six other sources and records
    no provenance, so subtracting the Plan set also lifted every other source's hold on the
    same names. Approving a plan handed back tools the operator had switched off by hand
    and shell tools to a thread whose mode must never reach the host. The level is read
    live at the gate instead (``services/tool_policy.py``), which leaves nothing to undo.

    Never past the thread's agent's ceiling: approving a plan widens the level, not the
    agent.
    """
    ctx.deps.permission = stricter_permission(level, ctx.deps.permission_ceiling or level)


def plan_toolset() -> FunctionToolset[RunDeps]:
    toolset: FunctionToolset[RunDeps] = FunctionToolset()

    @toolset.tool(name="enter")
    async def enter(ctx: RunContext[RunDeps], reason: str) -> str:
        """Switch this conversation into plan mode and stop acting.

        For work that is large, ambiguous, or expensive to get wrong — a change across
        several files, anything that rewrites or deletes what the operator has, a request
        you can read more than one way. Not for work you can simply do.

        Every tool that changes anything leaves your catalog until the operator approves
        a plan. Investigate by reading, then call `plan_submit`.

        `reason`: one line on why this needs a plan. The operator reads it.
        """
        plans = ctx.deps.caps.get_optional(PlanMode)
        conversation_id = ctx.deps.conversation_id
        if plans is None or conversation_id is None:
            return NO_THREAD
        if _unattended(ctx):
            return NO_OPERATOR
        _ = await plans.enter(conversation_id, reason=reason, run=ctx.deps.run)
        _retarget(ctx, PLANNING_LEVEL)
        return (
            "This conversation is now in plan mode: nothing you call can change anything. "
            "Investigate by reading, and ask the operator with `builtin_ask_user` wherever "
            "the request is open to more than one reading or a choice is theirs to make — "
            "planning on a guess is what makes a plan need rewriting. Then submit with "
            "`plan_submit`."
        )

    @toolset.tool(name="read")
    async def read(ctx: RunContext[RunDeps]) -> str:
        """The plan this conversation agreed to, in full. Read it while carrying one out
        when you want the reasoning behind a step: the task list holds the steps, this
        holds why they are the steps."""
        plans = ctx.deps.caps.get_optional(PlanMode)
        conversation_id = ctx.deps.conversation_id
        if plans is None or conversation_id is None:
            return "There is no plan for this run."
        plan = await plans.current(ctx.deps.run.owner_id, conversation_id)
        if plan is None:
            return "This conversation has no plan."
        steps = "\n".join(f"{i}. {step}" for i, step in enumerate(plan.steps, start=1))
        return f"# {plan.title}\n\n({plan.status})\n\n{plan.body}\n\n## Steps\n\n{steps}"

    @toolset.tool(name="submit")
    async def submit(
        ctx: RunContext[RunDeps],
        title: Annotated[str, Field(description="One line naming what this plan does.")],
        body: Annotated[
            str,
            Field(
                description=(
                    "The plan, in markdown: why, the approach, which files you would "
                    "touch and what changes in each, what you found that the operator "
                    "would not expect, how it would be verified. As long as the work "
                    "requires — the operator decides on this alone, and shortening it "
                    "is the one way to make it useless."
                )
            ),
        ],
        steps: Annotated[
            list[str],
            Field(
                min_length=1,
                description=(
                    "The ordered, concrete steps. They become the conversation's task "
                    "list on approval, so write each as the piece of work it is."
                ),
            ),
        ],
    ) -> str:
        """Submit this plan for the operator's approval, and pause until they answer.

        How a plan-mode turn ends. They approve, ask for changes, or reject. On approval
        the conversation moves to the Auto level, the steps become its task list, and you
        carry the plan out in this same turn — do not stop to ask whether to begin. On a
        request for changes, revise and submit again; the conversation stays in plan mode
        until they agree.
        """
        plans = ctx.deps.caps.get_optional(PlanMode)
        conversation_id = ctx.deps.conversation_id
        if plans is None or conversation_id is None:
            return NO_THREAD
        if _unattended(ctx):
            return NO_OPERATOR
        owner_id = ctx.deps.run.owner_id
        if not ctx.tool_call_approved:
            # Record and announce first, *then* park: the panel renders the plan the
            # operator is being asked about, and it has to exist before they are asked.
            recorded = await plans.submit(
                owner_id,
                conversation_id,
                title=title,
                body=body,
                steps=list(steps),
                run=ctx.deps.run,
            )
            if recorded is None:
                # Nothing was stored and nothing was announced (a locked vault). Parking
                # anyway would put the turn in front of an operator with no plan on screen
                # to read — the panel would say there is none while the dock pointed at it
                # — and leave Stop as the only way out. Say so instead.
                return NO_RECORD
            raise ApprovalRequired()
        _, level = await plans.approve(owner_id, conversation_id, run=ctx.deps.run)
        _retarget(ctx, ACTING_LEVEL)
        return (
            f"The operator approved this plan. The conversation is now at the {level} "
            "level and its task list holds your steps. Carry the plan out now, marking "
            "each task in_progress as you start it and completed as you finish it."
        )

    return toolset
