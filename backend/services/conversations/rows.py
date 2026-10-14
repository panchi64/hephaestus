"""Persistence records: the plaintext row a turn queues, the row read back for rehydration, and the
unit of work the write-behind drainer takes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import NamedTuple

from sqlalchemy import text as sa_text
from sqlmodel import Session


class Row(NamedTuple):
    """A persistence-ready message row, still plaintext. The drainer encrypts ``text`` +
    ``blob`` just before the write (lock-aware side of the queue), so a vault lock
    mid-turn parks the write rather than losing it."""

    id: str
    parent_id: str | None
    seq: int
    kind: str
    text: str
    blob: str
    attachment_ids: list[str]
    blocked_reason: str | None
    # Set only on a conversation-compaction checkpoint: the flag, plus the id of the last
    # node on the path its summary covers. See `record_compaction`.
    compacted: bool = False
    compacted_through: str | None = None
    # Wall-clock for a model response, from the run's own stopwatch. Null on requests.
    llm_ms: int | None = None
    ttft_ms: int | None = None
    tool_ms: int | None = None


class HydratedRow(NamedTuple):
    """One message row read back for rehydration, projected out of the ORM object so
    nothing is held across the session boundary.

    Named rather than a bare tuple: it is unpacked positionally at the far end of the
    read, and a plain 12-tuple makes adding a column a silent off-by-one that lands as
    a wrong *value* in a neighbouring field rather than an error."""

    id: str
    parent_id: str | None
    seq: int
    pinned: bool
    blob: str
    attachment_ids: list[str]
    blocked_reason: str | None
    compacted: bool
    compacted_through: str | None
    llm_ms: int | None
    ttft_ms: int | None
    tool_ms: int | None


@dataclass
class PersistJob:
    """A unit of durable work, drained in FIFO order so an active-leaf move or a
    delete always lands after the message writes it follows.

    - ``messages``: insert ``rows`` and set the active leaf (a completed turn).
    - ``active_leaf``: move the pointer only (regenerate/edit/rewind/switch).
    - ``delete``: remove ``deleted_ids`` (highest seq first, children before
      parents) and reseat the active leaf.
    """

    kind: str  # "messages" | "active_leaf" | "delete" | "pin" | "unblock" | "block"
    conversation_id: str
    active_leaf_id: str | None = None
    rows: list[Row] = field(default_factory=list)
    deleted_ids: list[str] = field(default_factory=list)
    # For "pin": the message to (un)pin and the value to set. "unblock" reuses
    # ``message_id`` — the turn whose stop marker the operator resolved — and "block"
    # sets ``blocked_reason`` on it.
    message_id: str | None = None
    pinned: bool = False
    blocked_reason: str | None = None
    # The active path's last-used model at queue time — written denormalized onto
    # the Conversation alongside its active_leaf_id, so the listing reads it without
    # opening a blob. Computed where the leaf moves, never in the drainer (the tree
    # may have moved on by then). Unused by the "pin" job, which leaves model alone.
    model: str | None = None


def defer_fk(session: Session) -> None:
    """Defer foreign-key enforcement to commit for this transaction, so a set of
    rows linked by the self-referential ``parent_id`` FK can be deleted in one
    statement regardless of order. SQLite checks FKs per-statement otherwise; this
    is its mechanism (a no-op guard keeps a non-SQLite backend from erroring on the
    pragma — such a backend would use deferrable constraints instead)."""
    if session.bind is not None and session.bind.dialect.name == "sqlite":
        _ = session.connection().execute(sa_text("PRAGMA defer_foreign_keys=ON"))
