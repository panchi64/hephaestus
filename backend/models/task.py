"""Routines — the operator's recurring/one-off/webhook automations, stored as scheduled
tasks.

A `ScheduledTask` is either an **agent** task (its prompt drives an ordinary Run by the
routine's agent, in a new thread each run) or a **reminder** (its prompt is
delivered verbatim as a notification body — no AI phrasing). At-rest posture mirrors
notifications: `title`/`prompt` are the operator's own content, so they are encrypted
under the vault; schedule shape, output channel, and timestamps are structural
metadata the scheduler needs to query in the clear.

`pre_authorized` is policy, not content — like `ApprovalGrant.tool_name` — so it stays
in the clear: the scoped pre-authorization (`AE-3.5`) a task's unattended runs get,
seeded into a conversation grant at each execution.

`webhook_token` is this task's own analogue of the previews proxy's unguessable path
token — "the token IS the credential", compared for equality on an auth-exempt route,
never decrypted content the operator needs recovered from ciphertext. It is stored
in the clear (like the previews token, and like a session token in `core.auth`), not
vault-sealed, so it can be read back into a `webhookUrl` on every listing, and rotated
by simply replacing the column.

`TaskRun` is one execution record. The scheduler inserts a row (`outcome=None`) the
moment it fires a task and finalizes `outcome`/`finished_at` once the executor (or
notify) settles — so a row with `finished_at is None` is a still-live execution, the
non-overlap check's source of truth alongside the scheduler's own in-memory tracking.
"""

from __future__ import annotations

import secrets
from datetime import datetime
from enum import StrEnum

from sqlalchemy import JSON, Column, ForeignKey, String
from sqlmodel import Field, SQLModel

from models._fields import new_id, utcnow


class TaskKind(StrEnum):
    AGENT = "agent"
    REMINDER = "reminder"


class ScheduleType(StrEnum):
    ONCE = "once"
    INTERVAL = "interval"
    CRON = "cron"
    WEBHOOK = "webhook"


class TaskOutput(StrEnum):
    CHAT = "chat"
    NOTIFICATION = "notification"


class TaskOutcome(StrEnum):
    OK = "ok"
    ERROR = "error"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"


def new_webhook_token() -> str:
    """An unguessable per-task webhook credential — same construction as the previews
    proxy token (`secrets.token_urlsafe(32)`, `services/sandbox/manager.py`)."""
    return secrets.token_urlsafe(32)


class ScheduledTask(SQLModel, table=True):
    __tablename__ = "scheduled_tasks"

    id: str = Field(default_factory=new_id, primary_key=True)
    owner_id: str = Field(index=True)
    # The project this belongs to, or null for **unfiled** — visible in every scope,
    # not orphaned. See models/project.py for the one scope rule.
    project_id: str | None = Field(default=None, index=True)
    kind: str = Field(index=True)  # TaskKind

    # AEAD ciphertext — the operator's own content, sealed like a notification's
    # title/body.
    title_enc: str
    prompt_enc: str

    # Schedule shape. Exactly one of {run_at, every_seconds, cron_expr} is meaningful,
    # selected by `schedule_type`; a `webhook` task uses none of them (it fires only
    # when its hook route is called, a later phase) and carries no `next_run_at`.
    schedule_type: str = Field(index=True)  # ScheduleType
    run_at: datetime | None = None
    # A float (not just whole seconds) so a short-interval test can drive the
    # scheduler's tick loop in well under a second, like the rest of this suite's
    # lock-aware workers do — a real recurring task still just passes a whole number.
    every_seconds: float | None = None
    cron_expr: str | None = None

    output: str  # TaskOutput
    # The scoped pre-authorization (AE-3.5) this task's unattended runs get — tool
    # names from the same vocabulary `ApprovalGrant.tool_name` uses. Policy, not
    # content, so it stays in the clear (like that grant's own scope).
    pre_authorized: list[str] = Field(sa_column=Column(JSON, nullable=False, default=list))

    enabled: bool = Field(default=True, index=True)
    # False for a routine an agent drafted: it never fires until the operator approves it.
    approved: bool = Field(default=True, index=True)

    # The agent each fire runs (`services/agents`), by name. Null is the default agent.
    agent_name: str | None = None
    # The thread its latest run opened. Null until it first runs.
    conversation_id: str | None = Field(default=None, index=True)
    # The IANA zone a cron schedule's fields are read in.
    timezone: str = Field(default="UTC")

    # Nullable: only a `webhook`-type task ever has one. See the module docstring for
    # why it is stored in the clear rather than vault-sealed.
    webhook_token: str | None = Field(default=None, unique=True, index=True)

    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    last_run_at: datetime | None = None
    # The scheduler's own due-query key: null means "never due" (a disabled task, a
    # spent `once`, or a `webhook` task). Indexed — the tick loop's hot read is
    # "every enabled task whose next_run_at has passed".
    next_run_at: datetime | None = Field(default=None, index=True)


class TaskRun(SQLModel, table=True):
    __tablename__ = "task_runs"

    id: str = Field(default_factory=new_id, primary_key=True)
    # Cascades: a task's execution history is meaningless once the task itself is gone.
    task_id: str = Field(
        sa_column=Column(
            String,
            ForeignKey("scheduled_tasks.id", ondelete="CASCADE"),
            index=True,
            nullable=False,
        )
    )
    # Set only for an `agent` task's execution — the Run it drove.
    run_id: str | None = Field(default=None, index=True)
    conversation_id: str | None = Field(default=None, index=True)
    started_at: datetime = Field(default_factory=utcnow, index=True)
    finished_at: datetime | None = None
    # Null while the execution is still live (between the scheduler's started-row
    # insert and its finalize) — never null once `finished_at` is set. TaskOutcome
    # when settled.
    outcome: str | None = None
    # AEAD ciphertext of a short outcome summary (the operator's content, like a
    # notification body) — set only once the execution finalizes.
    summary_enc: str | None = None
