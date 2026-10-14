"""The decrypted notification shape, shared by the in-app surface and its out-of-band
channels (a leaf, so neither imports the other)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class NotificationView:
    """A decrypted notification — the shape every read returns (the owner sees their
    own content in the clear, like documents/memory)."""

    id: str
    owner_id: str
    kind: str
    title: str
    body: str | None
    conversation_id: str | None
    run_id: str | None
    task_id: str | None
    created_at: datetime
    read_at: datetime | None
    resolved_at: datetime | None
