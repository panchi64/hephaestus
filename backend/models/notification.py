"""Notifications schema: the durable attention record.

Something happened while the operator wasn't watching (an approval is needed, a run
finished or failed unattended, a scheduled task's outcome) and must land somewhere durable
and actionable, unlike the per-run event stream (`runs/events/`) that dies with its run.

The content (``title`` and optional ``body``) is encrypted; kind, links, timestamps and
read/resolved state are structural metadata the DB indexes and orders by, so they stay in
the clear. ``task_id`` links a notification to its scheduled task: a reminder's fire, or an
agent task's outcome when its output channel is ``notification`` (see
`services/scheduler.py`).
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import ClassVar

from sqlmodel import Field

from models._fields import new_id, utcnow
from models._table import Table


class NotificationKind(StrEnum):
    """The in-code vocabulary for ``kind``, a plain str column to keep SQLite and Alembic simple."""

    APPROVAL_NEEDED = "approval_needed"
    RUN_COMPLETED = "run_completed"
    RUN_FAILED = "run_failed"
    SAVE_FAILED = "save_failed"
    # The scheduler's kinds: a reminder fires REMINDER (title = task title, body = the
    # prompt verbatim); a task with output channel "notification" fires TASK_OUTCOME at
    # terminal with a short summary.
    REMINDER = "reminder"
    TASK_OUTCOME = "task_outcome"
    SYSTEM = "system"
    VM_DOWN = "vm_down"


class Notification(Table, table=True):
    __tablename__: ClassVar[str] = "notifications"

    id: str = Field(default_factory=new_id, primary_key=True)
    owner_id: str = Field(index=True)
    kind: str = Field(index=True)
    # AEAD ciphertext: a short title always, an optional longer body (e.g. a tool's
    # plain-language explanation of what it would do).
    title_enc: str
    body_enc: str | None = None
    # Clear links so the frontend can deep-link without decrypting; all optional (a
    # `system` notification may name none).
    conversation_id: str | None = Field(default=None, index=True)
    run_id: str | None = Field(default=None, index=True)
    task_id: str | None = Field(default=None, index=True)
    created_at: datetime = Field(default_factory=utcnow, index=True)
    # When the operator saw it (mark-read, or opening its conversation). Independent of
    # resolution: an approval seen but undecided is read yet unresolved.
    read_at: datetime | None = None
    # When what it was about settled: an approval decided by any path, or its run reaching
    # terminal undecided. Only `approval_needed` is ever resolved; the others are born settled.
    resolved_at: datetime | None = None
