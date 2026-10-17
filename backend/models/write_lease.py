"""A thread's permission to write a folder of the agent's home it does not own.

Scratch is the thread's own. A project, or a shared folder, is the operator's: the agent
reads it freely and writes it once the operator has said yes, and that yes is a lease on
the folder for this conversation, until it lapses. A lease is policy, not content, so
nothing here is sealed.
"""

from __future__ import annotations

from datetime import datetime
from typing import ClassVar

from sqlalchemy import UniqueConstraint
from sqlmodel import Field

from models._fields import new_id, utcnow
from models._table import Table, TableArgs


class WriteLease(Table, table=True):
    __tablename__: ClassVar[str] = "write_leases"
    # One lease per (owner, conversation, place): leasing again refreshes its expiry.
    __table_args__: ClassVar[TableArgs] = (
        UniqueConstraint("owner_id", "conversation_id", "place", name="uq_write_lease_place"),
    )

    id: str = Field(default_factory=new_id, primary_key=True)
    owner_id: str = Field(index=True)
    conversation_id: str = Field(index=True)
    #: The folder, as its path under the home: `projects/<slug>` or `shared/<name>`.
    place: str
    created_at: datetime = Field(default_factory=utcnow)
    expires_at: datetime
