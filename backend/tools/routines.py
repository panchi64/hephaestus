"""The `routines` category: the model scheduling work for later or on repeat.

**What an agent writes is a draft.** A drafted routine never fires until the operator
approves it, and approving is where its pre-authorizations are accepted. Rewriting or
deleting an approved routine is the operator's to allow, unless the change only takes reach
away. The model reads the operator's phrase into a schedule; the code validates it and
answers with the fire times it produces, so a misread shows up before anything runs.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic_ai import AbstractToolset, FunctionToolset, ModelRetry, RunContext
from pydantic_ai.exceptions import ApprovalRequired

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


def _summary(routine: Routine) -> dict[str, Any]:
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
        raise ModelRetry(f"There is no routine {routine_id!r}; list them first.") from None


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
        """Draft a routine, or rewrite `routine_id`: `prompt` goes to `agent` on `schedule`,
        each time in a new thread.

        `schedule`: `once 2026-06-12T09:00+02:00`, `every 30m` (m/h/d), or
        `cron 0 9 * * 1-5 [zone]` (default: the operator's zone). `pre_authorized` names
        tools its turns may use without asking; `notify` reports each outcome. A new
        routine waits for the operator's approval; check `next_runs`.
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
            raise ModelRetry(f"That routine was rejected ({exc.field}): {exc}") from None
        _wake(ctx)
        return {"ok": True, **_summary(routine)}

    @toolset.tool(name="list")
    async def list_routines(ctx: RunContext[RunDeps]) -> dict[str, object]:
        """Every routine, with its id, agent, schedule, status and next fire times."""
        store = ctx.deps.caps.get_optional(RoutineStore)
        if store is None:
            return {"ok": False, "error": _UNAVAILABLE}
        return {"ok": True, "routines": [_summary(r) for r in await store.list(ctx.deps.owner_id)]}

    @toolset.tool
    async def delete(ctx: RunContext[RunDeps], routine_id: str) -> dict[str, object]:
        """Delete a routine. Its threads stay."""
        store = ctx.deps.caps.get_optional(RoutineStore)
        if store is None:
            return {"ok": False, "error": _UNAVAILABLE}
        routine = await _find(store, ctx, routine_id)
        if routine.approved and not ctx.tool_call_approved:
            raise ApprovalRequired()
        await store.delete(ctx.deps.owner_id, routine.id)
        return {"ok": True, "deleted": routine.title}

    @toolset.tool
    async def run_now(ctx: RunContext[RunDeps], routine_id: str) -> dict[str, object]:
        """Fire an approved routine once now, in a new thread, without waiting."""
        store = ctx.deps.caps.get_optional(RoutineStore)
        scheduler = ctx.deps.caps.get_optional(SchedulerService)
        if store is None or scheduler is None:
            return {"ok": False, "error": _UNAVAILABLE}
        routine = await _find(store, ctx, routine_id)
        if not routine.approved:
            return {"ok": False, "error": "It is a draft; the operator has not approved it."}
        if not routine.enabled:
            return {"ok": False, "error": "It is switched off."}
        await scheduler.fire_now(routine.id)
        return {"ok": True, "started": routine.title}

    return toolset


def _wake(ctx: RunContext[RunDeps]) -> None:
    scheduler = ctx.deps.caps.get_optional(SchedulerService)
    if scheduler is not None:
        scheduler.wake()
