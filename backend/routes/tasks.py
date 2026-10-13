"""The routines surface — CRUD, approval, manual fire, run history, natural-language
schedules, and the inbound webhook trigger. Routines are `ScheduledTask` rows; every write
goes through :class:`~services.routines.RoutineStore`, the same rules the agent's
`routines_*` tools apply. Out-shapes are camelCase.

``POST /tasks/hooks/{token}`` is the one auth-EXEMPT route here (``core/auth.py``
matches ``/tasks/hooks/`` exactly, mirroring the ``/previews`` token-gated-subtree
pattern) — the per-task unguessable ``webhook_token`` **is** the credential, compared
in constant time against every webhook-type task **the operator owns** rather than
resolved by an indexed equality lookup, so an inbound caller's timing can't narrow
down a valid token. Auth-exempt is not owner-exempt: the candidate scan carries the
same ``owner_id`` predicate every other query on this surface does.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Query, Request
from sqlmodel import Session, select

from core.db import in_session
from models.calendar import DEFAULT_TIMEZONE
from models.task import ScheduledTask, ScheduleType
from routes import deps
from routes.camel import CamelModel
from routes.deps import OPERATOR_ID
from services.projects import project_clause
from services.routines import Routine, RoutineRun, Schedule, parse_schedule, upcoming
from tools.catalog import approval_scopes

router = APIRouter(prefix="/tasks", tags=["tasks"])


# --- wire shapes -----------------------------------------------------------------


class TaskSchedule(CamelModel):
    """One routine's trigger — exactly one of ``run_at``/``every_seconds``/``cron`` is
    meaningful, selected by ``type``; a ``webhook`` schedule uses none of them. A cron's
    fields are read in ``timezone``."""

    type: str
    run_at: datetime | None = None
    every_seconds: float | None = None
    cron: str | None = None
    timezone: str = DEFAULT_TIMEZONE

    def to_schedule(self) -> Schedule:
        return Schedule(
            type=self.type,
            run_at=self.run_at,
            every_seconds=self.every_seconds,
            cron=self.cron,
            timezone=self.timezone,
        )


class TaskCreate(CamelModel):
    kind: str
    title: str
    prompt: str
    schedule: TaskSchedule
    output: str
    pre_authorized: list[str] = []
    agent_name: str | None = None
    enabled: bool = True


class TaskPatch(CamelModel):
    """Partial update — an omitted field is left unchanged. ``rotate_webhook_token``
    is a one-shot action (regenerate the credential), not a stored field."""

    title: str | None = None
    prompt: str | None = None
    schedule: TaskSchedule | None = None
    output: str | None = None
    pre_authorized: list[str] | None = None
    agent_name: str | None = None
    enabled: bool | None = None
    rotate_webhook_token: bool = False


class TaskOut(CamelModel):
    id: str
    kind: str
    title: str
    prompt: str
    schedule: TaskSchedule
    output: str
    pre_authorized: list[str]
    agent_name: str | None = None
    enabled: bool
    # False for a routine an agent drafted: it waits on the operator's approval.
    approved: bool
    # The routine's thread, once it has fired.
    conversation_id: str | None = None
    created_at: datetime
    updated_at: datetime
    last_run_at: datetime | None = None
    next_run_at: datetime | None = None
    # The next few fire times, so the schedule reads as instants.
    upcoming: list[datetime] = []
    # The relative webhook path (the frontend composes the absolute URL) — set only
    # for a webhook-type task.
    webhook_url: str | None = None


class TaskListOut(CamelModel):
    items: list[TaskOut]


class TaskRunOut(CamelModel):
    id: str
    task_id: str
    run_id: str | None = None
    conversation_id: str | None = None
    started_at: datetime
    finished_at: datetime | None = None
    outcome: str | None = None
    summary: str | None = None


class TaskRunListOut(CamelModel):
    items: list[TaskRunOut]


class RunNowOut(CamelModel):
    task_run_id: str


class TemplateOut(CamelModel):
    """A routine a published plugin offers; the operator sets it up through the form."""

    plugin: str
    name: str
    title: str
    prompt: str
    schedule: TaskSchedule
    agent_name: str | None = None


class TemplateListOut(CamelModel):
    items: list[TemplateOut]


class ParseScheduleIn(CamelModel):
    phrase: str
    timezone: str = DEFAULT_TIMEZONE


class ParseScheduleOut(CamelModel):
    schedule: TaskSchedule
    upcoming: list[datetime]


# --- helpers -------------------------------------------------------------------------


async def _validate_pre_authorized(request: Request, values: list[str]) -> None:
    """A routine may only pre-authorize (`AE-3.5`) a tool that can actually pause a run —
    the same vocabulary `ApprovalGrant.tool_name` uses. Derived from the live catalog,
    because external tools are named from the operator's own servers. A stored scope that
    stops existing simply stops matching; it is rejected only on write."""
    known = {
        s.name
        for s in await approval_scopes(
            deps.external(request),
            OPERATOR_ID,
            deps.tool_categories(request),
            deps.gated_tools(request),
        )
    }
    unknown = [v for v in values if v not in known]
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=f"unknown pre-authorization scope(s): {', '.join(sorted(unknown))}",
        )


def _schedule_out(schedule: Schedule) -> TaskSchedule:
    return TaskSchedule(
        type=schedule.type,
        run_at=schedule.run_at,
        every_seconds=schedule.every_seconds,
        cron=schedule.cron,
        timezone=schedule.timezone,
    )


def _task_out(routine: Routine) -> TaskOut:
    is_webhook = routine.schedule.type == ScheduleType.WEBHOOK.value
    return TaskOut(
        id=routine.id,
        kind=routine.kind,
        title=routine.title,
        prompt=routine.prompt,
        schedule=_schedule_out(routine.schedule),
        output=routine.output,
        pre_authorized=list(routine.pre_authorized),
        agent_name=routine.agent_name,
        enabled=routine.enabled,
        approved=routine.approved,
        conversation_id=routine.conversation_id,
        created_at=routine.created_at,
        updated_at=routine.updated_at,
        last_run_at=routine.last_run_at,
        next_run_at=routine.next_run_at,
        upcoming=upcoming(routine.schedule, datetime.now(UTC)) if routine.approved else [],
        webhook_url=(
            f"/tasks/hooks/{routine.webhook_token}"
            if is_webhook and routine.webhook_token
            else None
        ),
    )


def _task_run_out(run: RoutineRun) -> TaskRunOut:
    return TaskRunOut(
        id=run.id,
        task_id=run.task_id,
        run_id=run.run_id,
        conversation_id=run.conversation_id,
        started_at=run.started_at,
        finished_at=run.finished_at,
        outcome=run.outcome,
        summary=run.summary,
    )


# --- routes --------------------------------------------------------------------------


@router.get("", response_model=TaskListOut)
async def list_tasks(request: Request) -> TaskListOut:
    # Only the listing is scoped; the scheduler fires every due routine regardless of
    # what the operator happens to be looking at.
    scope = project_clause(ScheduledTask.project_id, await deps.project_scope(request))
    routines = await deps.routines(request).list(OPERATOR_ID, scope=scope)
    return TaskListOut(items=[_task_out(r) for r in routines])


@router.post("", status_code=201, response_model=TaskOut)
async def create_task(body: TaskCreate, request: Request) -> TaskOut:
    await _validate_pre_authorized(request, body.pre_authorized)
    routine = await deps.routines(request).create(
        OPERATOR_ID,
        {
            "kind": body.kind,
            "title": body.title,
            "prompt": body.prompt,
            "schedule": body.schedule.to_schedule(),
            "output": body.output,
            "pre_authorized": tuple(body.pre_authorized),
            "agent_name": body.agent_name,
            "enabled": body.enabled,
        },
        approved=True,
        # Filed under the active project; null is unfiled and visible everywhere.
        project_id=await deps.active_project(request),
    )
    deps.scheduler(request).wake()
    return _task_out(routine)


@router.patch("/{task_id}", response_model=TaskOut)
async def update_task(task_id: str, body: TaskPatch, request: Request) -> TaskOut:
    if body.pre_authorized is not None:
        await _validate_pre_authorized(request, body.pre_authorized)
    changes = body.model_dump(exclude_unset=True, exclude={"rotate_webhook_token"})
    if body.schedule is not None:
        changes["schedule"] = body.schedule.to_schedule()
    if body.pre_authorized is not None:
        changes["pre_authorized"] = tuple(body.pre_authorized)
    routine = await deps.routines(request).update(
        OPERATOR_ID, task_id, changes, rotate_webhook_token=body.rotate_webhook_token
    )
    deps.scheduler(request).wake()
    return _task_out(routine)


@router.post("/{task_id}/approve", response_model=TaskOut)
async def approve_task(task_id: str, request: Request) -> TaskOut:
    """Let a routine an agent drafted fire. Its pre-authorizations are checked here, since
    this is where the operator accepts them."""
    store = deps.routines(request)
    await _validate_pre_authorized(
        request, list((await store.get(OPERATOR_ID, task_id)).pre_authorized)
    )
    routine = await store.approve(OPERATOR_ID, task_id)
    deps.scheduler(request).wake()
    return _task_out(routine)


@router.delete("/{task_id}", status_code=204)
async def delete_task(task_id: str, request: Request) -> None:
    await deps.routines(request).delete(OPERATOR_ID, task_id)


@router.post("/{task_id}/run_now", status_code=202, response_model=RunNowOut)
async def run_now(task_id: str, request: Request) -> RunNowOut:
    """Fire the routine immediately, through the scheduler's own non-overlap path — a
    still-live previous execution records a ``skipped`` run rather than running twice."""
    routine = await deps.routines(request).get(OPERATOR_ID, task_id)
    if not routine.approved:
        raise HTTPException(status_code=409, detail="task is awaiting approval")
    if not routine.enabled:
        raise HTTPException(status_code=409, detail="task is disabled")
    task_run_id = await deps.scheduler(request).fire_now(task_id)
    if task_run_id is None:  # a delete or disable raced the fire
        raise HTTPException(status_code=404, detail="task not found")
    return RunNowOut(task_run_id=task_run_id)


@router.get("/{task_id}/runs", response_model=TaskRunListOut)
async def list_task_runs(
    task_id: str, request: Request, limit: int = Query(default=50, ge=1, le=200)
) -> TaskRunListOut:
    runs = await deps.routines(request).runs(OPERATOR_ID, task_id, limit=limit)
    return TaskRunListOut(items=[_task_run_out(run) for run in runs])


@router.get("/templates", response_model=TemplateListOut)
async def list_templates(request: Request) -> TemplateListOut:
    templates = await deps.plugins(request).routine_templates(OPERATOR_ID)
    return TemplateListOut(
        items=[
            TemplateOut(
                plugin=plugin,
                name=template.name,
                title=template.title,
                prompt=template.prompt,
                schedule=_schedule_out(parse_schedule(template.schedule, DEFAULT_TIMEZONE)),
                agent_name=template.agent,
            )
            for plugin, template in templates
        ]
    )


@router.post("/parse-schedule", response_model=ParseScheduleOut)
async def read_schedule(body: ParseScheduleIn, request: Request) -> ParseScheduleOut:
    """Read a phrase ("every weekday at 9") into a schedule, without saving anything."""
    schedule = await deps.schedule_parser(request).parse(body.phrase, timezone=body.timezone)
    return ParseScheduleOut(
        schedule=_schedule_out(schedule), upcoming=upcoming(schedule, datetime.now(UTC))
    )


@router.post("/hooks/{token}", status_code=202)
async def fire_webhook(token: str, request: Request) -> None:
    """Auth-exempt (see `core/auth.py`): the unguessable ``token`` in the path is the
    credential. Fires the matched task the same way ``run_now`` does; 404s (rather
    than 401/403) on an unknown token, so a prober can't distinguish "wrong token"
    from "no such route"."""
    engine = deps.db_engine(request)

    def work(session: Session) -> list[ScheduledTask]:
        return list(
            session.exec(
                select(ScheduledTask).where(
                    # Owner-scoped like every other query on this surface: this route
                    # has no authenticated caller to derive an owner from, so the scan
                    # is pinned to the operator explicitly. `fire_now` does no ownership
                    # check of its own (its docstring assumes the route already did), so
                    # an unscoped scan would let a token minted for one owner match — and
                    # fire — another owner's row the moment the `owner_id` seam carries
                    # more than one operator.
                    ScheduledTask.owner_id == OPERATOR_ID,
                    ScheduledTask.schedule_type == ScheduleType.WEBHOOK.value,
                )
            ).all()
        )

    candidates = await in_session(engine, work)
    matched = next(
        (
            candidate
            for candidate in candidates
            if candidate.webhook_token is not None
            and secrets.compare_digest(candidate.webhook_token, token)
        ),
        None,
    )
    if matched is None:
        raise HTTPException(status_code=404, detail="not found")
    await deps.scheduler(request).fire_now(matched.id)
