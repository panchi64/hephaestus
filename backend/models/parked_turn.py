"""A run parked on an approval or a question, written through so a restart can resume it.

One row per parked run, deleted when it resumes, is cancelled or ends. The continuation
(history, pending calls, binding) is the operator's conversation, so it is sealed.
"""

from __future__ import annotations

from datetime import datetime
from typing import ClassVar

from sqlmodel import Field

from models._fields import utcnow
from models._table import Table


class ParkedTurnRecord(Table, table=True):
    __tablename__: ClassVar[str] = "parked_turns"

    run_id: str = Field(primary_key=True)
    owner_id: str = Field(index=True)
    conversation_id: str = Field(index=True)
    #: The run kind (``chat``, ``subagent``, …), which picks the hook that rebuilds it.
    kind: str
    prompt_message_id: str | None = None
    #: The last event id the run's stream had issued, so a restored stream continues above it.
    last_event_id: int = 0
    parked_at: datetime = Field(default_factory=utcnow)
    #: AEAD ciphertext of the JSON continuation.
    payload_enc: str
