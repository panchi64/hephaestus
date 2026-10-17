"""The calls approved in a conversation, and who approved each one.

The Auto level's decision model reads them as context for the next call. The summary is
model-authored text and can carry personal data, so it is vault-sealed.
"""

from __future__ import annotations

from datetime import datetime
from typing import ClassVar

from sqlmodel import Field

from models._fields import new_id, utcnow
from models._table import Table


class ApprovedCall(Table, table=True):
    __tablename__: ClassVar[str] = "approved_calls"

    id: str = Field(default_factory=new_id, primary_key=True)
    owner_id: str = Field(index=True)
    conversation_id: str = Field(index=True)
    tool_call_id: str
    tool_name: str
    summary_enc: str
    # "operator", "grant" or "review" (`services/approval_ledger.py`).
    approved_by: str
    created_at: datetime = Field(default_factory=utcnow, index=True)
