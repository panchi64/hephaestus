"""The `routines` category: the model scheduling work for later or on repeat.

**What an agent writes is a draft.** A drafted routine never fires until the operator
approves it, and approving is where its pre-authorizations are accepted. Rewriting or
deleting an approved routine is the operator's to allow, unless the change only takes reach
away. The model reads the operator's phrase into a schedule; the code validates it and
answers with the fire times it produces, so a misread shows up before anything runs.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic_ai import AbstractToolset, FunctionToolset, ModelRetry, RunContext
from pydantic_ai.exceptions import ApprovalRequired

from core.diagnostics import render_rejection, render_tool_error
from core.exceptions import NotFoundError, RoutineValidationError
from models.calendar import DEFAULT_TIMEZONE
from services.routines import (
    Routine,
    RoutineStore,
    format_schedule,
    narrows,
    parse_schedule,
    upcoming,
)
from services.scheduler import SchedulerService

from .deps import RunDeps

#: Raised for approval only when the routine being rewritten or deleted is approved.
GATED_TOOLS: frozenset[str] = frozenset({"routines_write", "routines_delete"})

_UNAVAILABLE = "Routines are not available in this deployment."


def _summary(routine: Routine) -> dict[str, object]:
    status = "draft" if not routine.approved else "on" if routine.enabled else "off"
    return {
        "id": routine.id,
        "title": routine.title,
        "agent": routine.agent_name or "default",
        "schedule": format_schedule(routine.schedule),
        "status": status,
        "next_runs": [t.isoformat() for t in upcoming(routine.schedule, datetime.now(UTC))],
    }


async def _find(store: RoutineStore, ctx: RunContext[RunDeps], routine_id: str) -> Routine:
    try:
        return await store.get(ctx.deps.owner_id, routine_id)
    except NotFoundError:
        raise ModelRetry(
            render_tool_error(
                f"there is no routine {routine_id!r}",
                hints=["take an `id` from `routines_list`"],
            )
        ) from None


def routines_toolset() -> AbstractToolset[RunDeps]:
    toolset = FunctionToolset[RunDeps]()

    @toolset.tool
    async def write(
        ctx: RunContext[RunDeps],
        title: str,
        prompt: str,
        schedule: str,
        agent: str | None = None,
        pre_authorized: list[str] | None = None,
        notify: bool = False,
        enabled: bool = True,
        routine_id: str | None = None,
    ) -> dict[str, object]:
        """Drafts a routine, a prompt that runs on a schedule in a new thread each time,
        or rewrites an existing one. Use it when the operator asks for something to
        happen later or on repeat: a morning summary, a weekly check, a reminder.

        A new routine is a draft and never fires until the operator approves it, so tell
        them it is waiting for their approval rather than that it is set up. Rewriting an
        approved routine asks them again, unless the change only takes reach away.

        Returns the routine's `id`, `status` and `next_runs`, the next times it will
        fire. Read `next_runs` against what the operator asked for, because a misread
        schedule shows there before anything runs.

        Args:
            title: A short name the operator will recognize in their routines list.
            prompt: What the routine's agent is told each time. It starts with no
                memory of this conversation, so write it as a complete brief.
            schedule: `once 2026-06-12T09:00+02:00` (with the offset), `every 30m`
                (`m`, `h` or `d`), or `cron 0 9 * * 1-5 [zone]`, read in the operator's
                zone unless you name another.
            agent: The agent that runs it. Omit for the default.
            pre_authorized: Full tool names its runs may use without asking, such as
                `shell_run_command`. Nobody is watching when it fires, so a run that
                reaches an unlisted tool needing approval waits until the operator
                answers.
            notify: True to send the operator a notification with each outcome, rather
                than only leaving the thread.
            enabled: False to save it switched off.
            routine_id: The routine to rewrite, from `routines_list`. Omit to draft a
                new one.
        """
        store = ctx.deps.caps.get_optional(RoutineStore)
        if store is None:
            return {"ok": False, "error": _UNAVAILABLE}
        try:
            values = {
                "title": title,
                "prompt": prompt,
                "schedule": parse_schedule(schedule, DEFAULT_TIMEZONE),
                "agent_name": agent,
                "pre_authorized": tuple(pre_authorized or ()),
                "output": "notification" if notify else "chat",
                "enabled": enabled,
            }
            if routine_id is None:
                routine = await store.create(ctx.deps.owner_id, values, approved=False)
            else:
                routine = await _find(store, ctx, routine_id)
                if routine.approved and not ctx.tool_call_approved:
                    if not narrows(routine, values):
                        raise ApprovalRequired()
                routine = await store.update(ctx.deps.owner_id, routine.id, values)
        except RoutineValidationError as exc:
            raise ModelRetry(render_rejection("the routine", exc)) from None
        _wake(ctx)
        return {"ok": True, **_summary(routine)}

    @toolset.tool(name="list")
    async def list_routines(ctx: RunContext[RunDeps]) -> dict[str, object]:
        """Returns every routine, each with its `id`, `title`, `agent`, `schedule`,
        `status` (`draft`, `on` or `off`) and `next_runs`."""
        store = ctx.deps.caps.get_optional(RoutineStore)
        if store is None:
            return {"ok": False, "error": _UNAVAILABLE}
        return {"ok": True, "routines": [_summary(r) for r in await store.list(ctx.deps.owner_id)]}

    @toolset.tool
    async def delete(ctx: RunContext[RunDeps], routine_id: str) -> dict[str, object]:
        """Deletes a routine so it never fires again. The threads it already ran stay.
        Deleting an approved routine asks the operator first.

        Args:
            routine_id: The routine's `id`, from `routines_list`.
        """
        store = ctx.deps.caps.get_optional(RoutineStore)
        if store is None:
            return {"ok": False, "error": _UNAVAILABLE}
        routine = await _find(store, ctx, routine_id)
        if routine.approved and not ctx.tool_call_approved:
            raise ApprovalRequired()
        await store.delete(ctx.deps.owner_id, routine.id, by_run=ctx.deps.run.id)
        return {"ok": True, "deleted": routine.title}

    @toolset.tool
    async def run_now(ctx: RunContext[RunDeps], routine_id: str) -> dict[str, object]:
        """Fires an approved, switched-on routine once now, in a new thread, and returns
        at once without waiting for it to finish. Its schedule is unchanged.

        Use it when the operator wants to see a routine run, or to try one they just
        approved.

        Args:
            routine_id: The routine's `id`, from `routines_list`.
        """
        store = ctx.deps.caps.get_optional(RoutineStore)
        scheduler = ctx.deps.caps.get_optional(SchedulerService)
        if store is None or scheduler is None:
            return {"ok": False, "error": _UNAVAILABLE}
        routine = await _find(store, ctx, routine_id)
        if not routine.approved:
            return {"ok": False, "error": "It is a draft; the operator has not approved it."}
        if not routine.enabled:
            return {"ok": False, "error": "It is switched off."}
        _ = await scheduler.fire_now(routine.id)
        return {"ok": True, "started": routine.title}

    return toolset


def _wake(ctx: RunContext[RunDeps]) -> None:
    scheduler = ctx.deps.caps.get_optional(SchedulerService)
    if scheduler is not None:
        scheduler.wake()
