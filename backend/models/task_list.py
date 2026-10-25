"""The agent's task list for a conversation — one row per thread.

The model keeps its own list of steps while it works (`Planning`, `pydantic_ai_harness`),
and the operator watches it in chat. That list has to outlive the run that produced it: a
browser reload mid-turn, a run parked on an approval, a resumed stream, and the next turn
in the same thread all expect the same tasks to still be there. So it is persisted rather
than held for the life of one run, keyed the way the sandbox workspace is keyed — by
conversation.

**Not to be confused with `plugins/routines/models.py`**, which is the *scheduled* task feature — a
cron-shaped thing the operator configures. This is the agent's own working checklist, and
nothing but a conversation ever owns one.

**One row, not one per task.** The whole list is read and rewritten together on every
mutation (the store's contract is list-shaped), it is small, and it is sealed as a unit —
a row per task would buy nothing and cost a join plus per-row sealing. The items are a
sealed JSON array: a task list is a verbatim description of what the operator asked for,
which is content, not policy (`XC-SEC-3`).
"""

from __future__ import annotations

from datetime import datetime
from typing import ClassVar

from sqlmodel import Field

from models._fields import new_id, utcnow
from models._table import Table


class ConversationTaskList(Table, table=True):
    __tablename__: ClassVar[str] = "conversation_tasks"

    id: str = Field(default_factory=new_id, primary_key=True)
    owner_id: str = Field(index=True)
    # Unique: a conversation has exactly one task list, and the store upserts against it.
    conversation_id: str = Field(index=True, unique=True)
    # AEAD ciphertext of the JSON array of task items (id, content, status, active_form,
    # parent_id, depends_on).
    items_enc: str
    # A sha256 over the sorted text of the open tasks the model was last reminded of, so the
    # same open set is never raised twice. Sealed: a bare hash of short task text can be
    # guessed back.
    reminded_open_enc: str | None = Field(default=None)
    updated_at: datetime = Field(default_factory=utcnow, index=True)
