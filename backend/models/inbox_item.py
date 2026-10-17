"""A conversation's inbox, written through so a restart loses nothing (``runs/inbox.py``).

One row per waiting message, deleted the moment the model takes it. The text and the
payload are sealed: a queued message is the operator's or an agent's words.
"""

from __future__ import annotations

from datetime import datetime
from typing import ClassVar

from sqlmodel import Field

from models._fields import utcnow
from models._table import Table


class InboxItem(Table, table=True):
    __tablename__: ClassVar[str] = "conversation_inbox"

    id: str = Field(primary_key=True)
    owner_id: str = Field(index=True)
    conversation_id: str = Field(index=True)
    seq: int
    source: str
    sender: str | None = Field(default=None)
    held: bool = Field(default=False)
    notice: str | None = Field(default=None)
    queued_at: datetime = Field(default_factory=utcnow)
    text_enc: str
    # AEAD ciphertext of the JSON payload (attachments, file references, command), when any.
    payload_enc: str | None = Field(default=None)


class InboxWaiting(Table, table=True):
    """A conversation whose operator stopped a turn: nothing in its inbox starts a turn
    until they act again. The row exists only while that holds."""

    __tablename__: ClassVar[str] = "conversation_inbox_waiting"

    conversation_id: str = Field(primary_key=True)
    owner_id: str = Field(index=True)
    # The next turn has not yet been told about the stop.
    stopped: bool = Field(default=False)
