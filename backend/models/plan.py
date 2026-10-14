"""The plan a thread produced for the operator to approve — one row per conversation.

A *plan* here is the written document a Plan-level turn ends in: what the agent would do,
concretely enough to say yes to. It is not the agent's running checklist — that is
``models/task_list.py``, and keeping the two words apart is the whole reason both files
exist.

**One row, replaced on every revision.** A plan that came back with feedback is superseded
by the plan that answers it, and the superseded one is not a thing anyone asks for: the
transcript already holds every version as the arguments of the call that submitted it, with
the operator's response beside it, which is the version history that is actually read.
``revision`` counts up so the panel can tell a resubmission from a redraw.

**Sealed.** A plan restates what the operator asked for and names their files — content,
not policy (`XC-SEC-3`) — so the document is AEAD ciphertext like the task list beside it.
``status`` and ``revision`` stay in the clear: they are how the surface decides what to
render, they say nothing about the work, and a locked vault should still be able to report
that a thread is waiting on an answer.
"""

from __future__ import annotations

from datetime import datetime

from sqlmodel import Field

from models._fields import new_id, utcnow
from models._table import Table


class ConversationPlan(Table, table=True):
    __tablename__ = "conversation_plans"

    id: str = Field(default_factory=new_id, primary_key=True)
    owner_id: str = Field(index=True)
    # Unique: a conversation has one plan at a time, and the store upserts against it.
    conversation_id: str = Field(index=True, unique=True)
    # AEAD ciphertext of `{title, body, steps}`.
    plan_enc: str
    # pending | approved | revising | denied. A plain string for the same reason the
    # permission level on the conversation row is one: a restored backup, or a row written
    # by another build, must still load.
    status: str = Field(default="pending", index=True)
    # Bumped on every submission, including a resubmission after feedback.
    revision: int = Field(default=1)
    updated_at: datetime = Field(default_factory=utcnow, index=True)
