"""A git worktree a thread opened on one of the operator's projects.

The thread works on its own branch there, mounted in its home at ``~/worktrees/<slug>``,
and the operator's checkout sees none of it until they merge. A row exists while the
worktree is open; merging or discarding it removes the row. Policy, not content, so
nothing here is sealed.
"""

from __future__ import annotations

from datetime import datetime
from typing import ClassVar

from sqlalchemy import UniqueConstraint
from sqlmodel import Field

from models._fields import new_id, utcnow
from models._table import Table, TableArgs


class ThreadWorktree(Table, table=True):
    __tablename__: ClassVar[str] = "thread_worktrees"
    # One open worktree per thread.
    __table_args__: ClassVar[TableArgs] = (
        UniqueConstraint("conversation_id", name="uq_thread_worktree_conversation"),
    )

    id: str = Field(default_factory=new_id, primary_key=True)
    owner_id: str = Field(index=True)
    conversation_id: str = Field(index=True)
    project_id: str = Field(index=True)
    created_at: datetime = Field(default_factory=utcnow)
