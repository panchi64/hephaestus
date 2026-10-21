"""Notifications schema — the durable attention record.

The backend chassis's cross-cutting attention surface: something happened while the
operator wasn't watching (a sensitive action needs approval, a run finished or failed
unattended, later a scheduled task's outcome) and it needs to land somewhere durable and
actionable — distinct from the frozen per-run event stream (`runs/events/`), which
dies with its run.

At-rest posture mirrors documents/memory: the content the notification *is* — its
``title`` and optional ``body`` — is encrypted; everything else (kind, links, timestamps,
read/resolved state) is structural metadata the DB can index and order by, so it stays in
the clear. ``task_id`` links a notification back to the scheduled task it's about — a
reminder's own fire, or an agent task's outcome when its output channel is
``notification`` (`app.py`'s task executor/notify closures write it; see
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
    """The in-code vocabulary for ``kind`` — a plain str column, like the other small
    enumerations in this package (``DocumentVersionOrigin``, …), so SQLite + Alembic
    stay simple."""

    APPROVAL_NEEDED = "approval_needed"
    RUN_COMPLETED = "run_completed"
    RUN_FAILED = "run_failed"
    SAVE_FAILED = "save_failed"
    # The scheduler's two kinds (`app.py`'s task executor/notify closures): a
    # reminder task fires REMINDER directly (title = task title, body = the prompt
    # verbatim); any task whose output channel is "notification" also fires
    # TASK_OUTCOME at terminal with a short outcome summary.
    REMINDER = "reminder"
    TASK_OUTCOME = "task_outcome"
    SYSTEM = "system"
    VM_DOWN = "vm_down"


class Notification(Table, table=True):
    __tablename__: ClassVar[str] = "notifications"

    id: str = Field(default_factory=new_id, primary_key=True)
    owner_id: str = Field(index=True)
    kind: str = Field(index=True)
    # AEAD ciphertext of the notification's content — a short title always, an optional
    # longer body (e.g. a tool's plain-language explanation of what it would do).
    title_enc: str
    body_enc: str | None = None
    # What the notification is about, when it's about something — clear links so the
    # frontend can deep-link without decrypting first. All optional: a `system`
    # notification may name none of them.
    conversation_id: str | None = Field(default=None, index=True)
    run_id: str | None = Field(default=None, index=True)
    # Nullable seam for the scheduler's task outcomes (a later phase) — unused until then.
    task_id: str | None = Field(default=None, index=True)
    created_at: datetime = Field(default_factory=utcnow, index=True)
    # Set when the operator has seen it (a REST mark-read, or opening the conversation
    # it's linked to). Independent of resolution — a read notification can still be
    # unresolved (e.g. an approval the operator saw but hasn't decided yet).
    read_at: datetime | None = None
    # Set when the thing the notification was about reached a settled state — an
    # approval decided (by any path), or its run reaching terminal without one ever
    # being decided. Only `approval_needed` notifications are ever resolved; the
    # others are born already-settled and never gain a `resolved_at`.
    resolved_at: datetime | None = None
