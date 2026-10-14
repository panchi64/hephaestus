"""Conversation store — the in-memory working tree + write-behind to the DB.

While a conversation is active its full message **tree** lives in memory (the fast
working set), so a turn continues with zero DB reads on the hot path. As each turn
completes, its new messages are copied onto a queue that a background drainer
writes to the DB off the critical path. The DB is the durable record; memory is
the fast one. A cold conversation rehydrates from the DB once, then runs at memory
speed.

The tree is what makes regenerate / edit / rewind work. Every node points at its
predecessor (``parent_id``); **siblings sharing a parent are versions** — with
compaction checkpoints **transparent**, so a version set reads through one rather
than stopping at it (``Tree.siblings``). The
conversation's *active leaf* is the tip of the path the operator is viewing —
walking it to the root yields the active history (the flat list the agent runs
against). Navigation never invents history: regenerate/edit/rewind all just move
the active leaf, then a normal turn records its messages as a new branch off it.

Content is **encrypted at rest**: the durable text and blob are encrypted by the
drainer, just before the write, not on the hot path. The working set stays
plaintext (it already holds plaintext in memory); the hot path only projects and
serializes. Encrypting in the drainer keeps it on the **lock-aware** side of the
queue — if the vault locks mid-turn the write parks until unlock instead of
erroring and losing the turn. Structural metadata (ids, parent ids, timestamps,
owner, seq, kind, the active-leaf pointer) stays plaintext so the DB can still
index and order. The drainer is a lock-aware :class:`~core.worker.WriteBehindWorker`
— it parks while the vault is locked and retries failed writes rather than
dropping them. Active-leaf moves and deletes ride the same queue so they stay
ordered behind the message writes that precede them.

The **title** is sealed too, but on the write path rather than in the drainer: it
is set by a rename or by auto-titling, both of which already run with the vault
unlocked, and neither rides the queue. It is user content — an auto-generated
summary of the operator's own first message, the most revealing single line a
thread has (`XC-SEC-3`). Rows written before it was sealed still carry the legacy
cleartext ``Conversation.title``; every read goes through ``_open_title``, which
prefers the ciphertext and falls back, and the startup backfill (``services/sealing``)
drains the cleartext away once the vault is unlocked.
"""

from __future__ import annotations

from .paths import ReplayHistory
from .projection import install_persisted_attachments
from .records import CompactionPlan, ConversationBinding, ConversationSummaryView, TurnStamps
from .store import ConversationStore
from .usage import (
    ConversationTotals,
    context_footprint,
    conversation_totals,
    footprint_or_estimate,
    last_request_usage,
)

__all__ = [
    "CompactionPlan",
    "ConversationBinding",
    "ConversationStore",
    "ConversationSummaryView",
    "ConversationTotals",
    "ReplayHistory",
    "TurnStamps",
    "context_footprint",
    "conversation_totals",
    "footprint_or_estimate",
    "install_persisted_attachments",
    "last_request_usage",
]
