"""A file a thread changed in the operator's own folder of a project, and what it held
before.

One row per (thread, project, file): the first change keeps the file as it was, so the
operator can put it back whatever the thread did to it after. A worktree's changes are not
here — they stay on its branch until a merge. The path and the original content are the
operator's own, so both are sealed; a row is found by unsealing the paths of one thread's
changes to one project, which is a short list. Content hashes stay in the clear, as a
workspace blob's do.
"""

from __future__ import annotations

from datetime import datetime
from typing import ClassVar

from sqlmodel import Field

from models._fields import new_id, utcnow
from models._table import Table


class ThreadChange(Table, table=True):
    __tablename__: ClassVar[str] = "thread_changes"

    id: str = Field(default_factory=new_id, primary_key=True)
    owner_id: str = Field(index=True)
    #: The thread whose changes these are: a spawned agent's land under the thread whose
    #: files it worked in.
    conversation_id: str = Field(index=True)
    project_id: str = Field(index=True)
    #: The file's path inside the project, sealed.
    path_enc: str
    #: The file before the thread's first change, sealed; null when it did not exist, or
    #: when it was too large to keep (``original_kept`` is then false).
    original_enc: bytes | None = None
    original_sha: str | None = None
    original_kept: bool = True
    #: The file as the thread last left it; null when the thread deleted it.
    current_sha: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    #: When the operator put the file back. A later change records afresh.
    reverted_at: datetime | None = None
