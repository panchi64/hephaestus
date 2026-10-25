"""Routines: scheduled work the operator, or an agent on their behalf, sets up.

A routine is a `ScheduledTask` row. The operator's own are approved as they are written; an
agent's is a draft that never fires until the operator approves it. Every read and write of
the table outside the scheduler's own bookkeeping goes through here, so the REST surface
and the agent's tools apply the same rules.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Collection, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import cast

from sqlalchemy import Engine
from sqlalchemy.sql.elements import ColumnElement
from sqlmodel import Session, col, select

from core.db import in_session
from core.exceptions import NotFoundError, RoutineValidationError
from core.serde import as_utc
from core.vault import Vault
from models._fields import utcnow
from services.agents import AgentCatalog

from ..models import (
    ScheduledTask,
    ScheduleType,
    TaskKind,
    TaskOutput,
    TaskRun,
    new_webhook_token,
)
from .schedule import Schedule, check_schedule

_KINDS = frozenset(k.value for k in TaskKind)
_OUTPUTS = frozenset(o.value for o in TaskOutput)

#: The fields a write may change.
FIELDS = frozenset(
    {"kind", "title", "prompt", "schedule", "output", "pre_authorized", "agent_name", "enabled"}
)


@dataclass(frozen=True)
class Routine:
    id: str
    kind: str
    title: str
    prompt: str
    schedule: Schedule
    output: str
    pre_authorized: tuple[str, ...]
    agent_name: str | None
    enabled: bool
    approved: bool
    conversation_id: str | None
    webhook_token: str | None
    created_at: datetime
    updated_at: datetime
    last_run_at: datetime | None
    next_run_at: datetime | None


@dataclass(frozen=True)
class RoutineRun:
    id: str
    task_id: str
    run_id: str | None
    conversation_id: str | None
    started_at: datetime
    finished_at: datetime | None
    outcome: str | None
    summary: str | None


def _text(values: Mapping[str, object], key: str) -> str:
    value = values[key]
    if not isinstance(value, str):
        raise RoutineValidationError(key, f"{key} must be text")
    return value


def _optional_text(values: Mapping[str, object], key: str) -> str | None:
    value = values[key]
    if value is not None and not isinstance(value, str):
        raise RoutineValidationError(key, f"{key} must be text")
    return value


def _flag(values: Mapping[str, object], key: str) -> bool:
    value = values[key]
    if not isinstance(value, bool):
        raise RoutineValidationError(key, f"{key} must be true or false")
    return value


def _names(value: object, key: str) -> list[str]:
    if not isinstance(value, list | tuple | set | frozenset):
        raise RoutineValidationError(key, f"{key} must be a list")
    return [str(item) for item in cast("Collection[object]", value)]


def _schedule(values: Mapping[str, object]) -> Schedule:
    value = values["schedule"]
    if not isinstance(value, Schedule):
        raise RoutineValidationError("schedule", "schedule must be a schedule")
    return value


def narrows(routine: Routine, changes: Mapping[str, object]) -> bool:
    """Whether ``changes`` only take reach away: switching the routine off, or dropping
    pre-authorizations."""
    for key, value in changes.items():
        if key == "enabled" and value is False:
            continue
        if key == "pre_authorized" and set(_names(value, key)) <= set(routine.pre_authorized):
            continue
        if getattr(routine, key) == value:
            continue
        return False
    return True


class RoutineStore:
    def __init__(self, engine: Engine, vault: Vault, agents: AgentCatalog | None = None):
        self._engine: Engine = engine
        self._vault: Vault = vault
        self._agents: AgentCatalog | None = agents
        self._before_delete: Callable[[str, str | None], Awaitable[None]] | None = None

    @property
    def engine(self) -> Engine:
        return self._engine

    @property
    def vault(self) -> Vault:
        """The vault that seals routine fields."""
        return self._vault

    # --- reads ------------------------------------------------------------------------

    async def list(
        self, owner_id: str, *, scope: ColumnElement[bool] | None = None
    ) -> list[Routine]:
        def work(session: Session) -> list[ScheduledTask]:
            query = select(ScheduledTask).where(ScheduledTask.owner_id == owner_id)
            if scope is not None:
                query = query.where(scope)
            return list(session.exec(query.order_by(col(ScheduledTask.created_at))).all())

        return [self._view(row) for row in await in_session(self._engine, work)]

    async def get(self, owner_id: str, routine_id: str) -> Routine:
        return self._view(await self._row(owner_id, routine_id))

    async def runs(self, owner_id: str, routine_id: str, *, limit: int = 50) -> list[RoutineRun]:
        _ = await self._row(owner_id, routine_id)

        def work(session: Session) -> list[TaskRun]:
            return list(
                session.exec(
                    select(TaskRun)
                    .where(TaskRun.task_id == routine_id)
                    .order_by(col(TaskRun.started_at).desc())
                    .limit(limit)
                ).all()
            )

        return [
            RoutineRun(
                id=run.id,
                task_id=run.task_id,
                run_id=run.run_id,
                conversation_id=run.conversation_id,
                started_at=run.started_at,
                finished_at=run.finished_at,
                outcome=run.outcome,
                summary=(self._vault.decrypt_str(run.summary_enc) if run.summary_enc else None),
            )
            for run in await in_session(self._engine, work)
        ]

    # --- writes -----------------------------------------------------------------------

    async def create(
        self,
        owner_id: str,
        values: Mapping[str, object],
        *,
        approved: bool,
        project_id: str | None = None,
    ) -> Routine:
        """``values`` carries ``title``, ``prompt`` and ``schedule``, and any other of
        :data:`FIELDS`."""
        values = {
            "kind": TaskKind.AGENT.value,
            "output": TaskOutput.CHAT.value,
            "pre_authorized": (),
            "agent_name": None,
            "enabled": True,
            **values,
        }
        await self._check(owner_id, values)
        schedule = _schedule(values)
        row = ScheduledTask(
            owner_id=owner_id,
            project_id=project_id,
            kind=_text(values, "kind"),
            title_enc=self._vault.encrypt_str(_text(values, "title")),
            prompt_enc=self._vault.encrypt_str(_text(values, "prompt")),
            schedule_type=schedule.type,
            output=_text(values, "output"),
            pre_authorized=_names(values["pre_authorized"], "pre_authorized"),
            agent_name=_optional_text(values, "agent_name"),
            enabled=_flag(values, "enabled"),
            approved=approved,
        )
        _set_schedule(row, schedule)

        def work(session: Session) -> ScheduledTask:
            session.add(row)
            session.flush()
            session.refresh(row)
            return row

        return self._view(await in_session(self._engine, work))

    async def update(
        self,
        owner_id: str,
        routine_id: str,
        changes: Mapping[str, object],
        *,
        approved: bool | None = None,
        rotate_webhook_token: bool = False,
    ) -> Routine:
        """Apply ``changes`` (a subset of :data:`FIELDS`). ``approved`` sets the approval
        too; left ``None``, it stays as it was."""
        await self._check(owner_id, changes)
        current = await self.get(owner_id, routine_id)
        schedule = _schedule(changes) if "schedule" in changes else None
        if rotate_webhook_token and (schedule or current.schedule).type != ScheduleType.WEBHOOK:
            raise RoutineValidationError(
                "schedule", "rotateWebhookToken only applies to a webhook-type task"
            )

        def work(session: Session) -> ScheduledTask:
            row = _owned(session, owner_id, routine_id)
            if "kind" in changes:
                row.kind = _text(changes, "kind")
            if "output" in changes:
                row.output = _text(changes, "output")
            if "agent_name" in changes:
                row.agent_name = _optional_text(changes, "agent_name")
            if "enabled" in changes:
                row.enabled = _flag(changes, "enabled")
            if "title" in changes:
                row.title_enc = self._vault.encrypt_str(_text(changes, "title"))
            if "prompt" in changes:
                row.prompt_enc = self._vault.encrypt_str(_text(changes, "prompt"))
            if "pre_authorized" in changes:
                row.pre_authorized = _names(changes["pre_authorized"], "pre_authorized")
            if schedule is not None:
                _set_schedule(row, schedule)
            if rotate_webhook_token:
                row.webhook_token = new_webhook_token()
            if approved is not None:
                row.approved = approved
                if approved and schedule is None:
                    _set_schedule(row, _schedule_of(row))
            row.updated_at = utcnow()
            session.add(row)
            session.flush()
            session.refresh(row)
            return row

        return self._view(await in_session(self._engine, work))

    async def approve(self, owner_id: str, routine_id: str) -> Routine:
        """Let a drafted routine fire, counting its schedule from now."""
        return await self.update(owner_id, routine_id, {}, approved=True)

    def before_delete(self, hook: Callable[[str, str | None], Awaitable[None]]) -> None:
        """Run ``hook`` with a routine's id, and the run deleting it, before it is deleted,
        so what is still running for it can be stopped while the routine's records exist."""
        self._before_delete = hook

    async def delete(self, owner_id: str, routine_id: str, *, by_run: str | None = None) -> None:
        """Delete a routine. ``by_run`` is the run asking, which is left running."""
        _ = await self._row(owner_id, routine_id)
        if self._before_delete is not None:
            await self._before_delete(routine_id, by_run)

        def work(session: Session) -> None:
            session.delete(_owned(session, owner_id, routine_id))

        await in_session(self._engine, work)

    async def attach_thread(self, routine_id: str, conversation_id: str) -> None:
        """Record the thread a routine's latest run opened."""

        def work(session: Session) -> None:
            row = session.get(ScheduledTask, routine_id)
            if row is not None:
                row.conversation_id = conversation_id
                session.add(row)

        await in_session(self._engine, work)

    # --- helpers ------------------------------------------------------------------------

    async def _row(self, owner_id: str, routine_id: str) -> ScheduledTask:
        return await in_session(self._engine, lambda s: _owned(s, owner_id, routine_id))

    async def _check(self, owner_id: str, values: Mapping[str, object]) -> None:
        unknown = set(values) - FIELDS
        if unknown:
            raise RoutineValidationError(sorted(unknown)[0], "not a routine field")
        if "kind" in values and values["kind"] not in _KINDS:
            raise RoutineValidationError("kind", f"unknown kind {values['kind']!r}")
        if "output" in values and values["output"] not in _OUTPUTS:
            raise RoutineValidationError("output", f"unknown output {values['output']!r}")
        for key in ("title", "prompt"):
            if key in values and not str(values[key]).strip():
                raise RoutineValidationError(key, f"{key} must not be empty")
        if "schedule" in values:
            check_schedule(_schedule(values))
        name = values.get("agent_name")
        if name is not None and self._agents is not None:
            known = await self._agents.agents(owner_id, include_drafts=True)
            if name not in known:
                raise RoutineValidationError("agent", f"there is no agent called {name!r}")

    def _view(self, row: ScheduledTask) -> Routine:
        return Routine(
            id=row.id,
            kind=row.kind,
            title=self._vault.decrypt_str(row.title_enc),
            prompt=self._vault.decrypt_str(row.prompt_enc),
            schedule=_schedule_of(row),
            output=row.output,
            pre_authorized=tuple(row.pre_authorized),
            agent_name=row.agent_name,
            enabled=row.enabled,
            approved=row.approved,
            conversation_id=row.conversation_id,
            webhook_token=row.webhook_token,
            created_at=row.created_at,
            updated_at=row.updated_at,
            last_run_at=row.last_run_at,
            next_run_at=row.next_run_at,
        )


def _owned(session: Session, owner_id: str, routine_id: str) -> ScheduledTask:
    row = session.get(ScheduledTask, routine_id)
    if row is None or row.owner_id != owner_id:
        raise NotFoundError("routine not found")
    return row


def _schedule_of(row: ScheduledTask) -> Schedule:
    return Schedule(
        type=row.schedule_type,
        run_at=as_utc(row.run_at),
        every_seconds=row.every_seconds,
        cron=row.cron_expr,
        timezone=row.timezone,
    )


def _set_schedule(row: ScheduledTask, schedule: Schedule) -> None:
    row.schedule_type = schedule.type
    row.run_at = schedule.run_at
    row.every_seconds = schedule.every_seconds
    row.cron_expr = schedule.cron
    row.timezone = schedule.timezone
    if schedule.type == ScheduleType.WEBHOOK:
        row.next_run_at = None
        row.webhook_token = row.webhook_token or new_webhook_token()
    else:
        row.webhook_token = None
        row.next_run_at = schedule.next_after(utcnow())
