"""Which conversation a messaging channel's address continues: one rolling thread per DM
or channel, until the operator starts a fresh one there."""

from __future__ import annotations

from datetime import datetime
from typing import ClassVar

from sqlmodel import Field, UniqueConstraint

from models._fields import new_id, utcnow
from models._table import Table, TableArgs


class ChannelLink(Table, table=True):
    __tablename__: ClassVar[str] = "channel_links"
    __table_args__: ClassVar[TableArgs] = (
        UniqueConstraint("owner_id", "channel", "address", name="uq_channel_links_address"),
    )

    id: str = Field(default_factory=new_id, primary_key=True)
    owner_id: str = Field(index=True)
    #: The messaging channel's key, e.g. ``discord``.
    channel: str
    #: Where on that channel, in its own terms (a DM or a channel id).
    address: str
    conversation_id: str = Field(index=True)
    created_at: datetime = Field(default_factory=utcnow)
