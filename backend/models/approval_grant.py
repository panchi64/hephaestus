"""Conversation-scoped tool auto-approval grants.

A grant records the operator's decision to auto-approve a given (namespaced) tool's
deferred calls within one conversation, for a bounded window (the TTL). While a grant is
active the engine resolves that tool's approval requests without re-prompting; an expired
or absent grant falls back to the strict per-call approval default. Grants are operator
policy, not content — structural fields only, so nothing here is vault-sealed — and
owner-scoped like every record.

A grant on a tool that *runs a command* is scoped narrower than the tool
(:attr:`ApprovalGrant.command_prefix`): "stop asking me about `uv run pytest`" is what the
operator means when they tick the box under a test run, and one yes to
``shell_run_command`` is not a yes to every command a thread will ever want. The operator
can still say the wider thing deliberately, and :attr:`ApprovalGrant.decisive` is what
records that they did.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import UniqueConstraint
from sqlmodel import Field

from models._fields import new_id, utcnow
from models._table import Table


class ApprovalGrant(Table, table=True):
    __tablename__ = "approval_grants"
    # One grant per (owner, conversation, tool, command prefix): re-granting refreshes its
    # expiry.
    __table_args__ = (
        UniqueConstraint(
            "owner_id",
            "conversation_id",
            "tool_name",
            "command_prefix",
            name="uq_approval_grant_scope",
        ),
    )

    id: str = Field(default_factory=new_id, primary_key=True)
    owner_id: str = Field(index=True)
    conversation_id: str = Field(index=True)
    # The namespaced tool name auto-approved in this conversation (e.g. "corpus_retrieve").
    tool_name: str
    # The command this grant is scoped to, as the JSON list of leading words the scope was
    # read off (`["uv", "run", "pytest"]`); `""` covers the whole tool. Encoded and decoded
    # in one place (`services/approval_grants.py`) — nothing else sees the JSON.
    #
    # **Not nullable, and empty rather than NULL**: SQLite treats NULLs as distinct in a
    # UNIQUE index, so a nullable column would stop the re-grant upsert's ON CONFLICT from
    # ever matching a whole-tool grant, and every refresh would insert a second row.
    command_prefix: str = Field(default="")
    # Whether this grant *answers* for the operator wherever it applies, or only informs
    # the level that is doing the deciding. A grant has always settled a call at the levels
    # that ask outright; at the level that reviews, one recorded from a single approval is
    # an input to the review rather than a way past it. This column is how the operator
    # says the wider thing on purpose: a standing yes to the whole tool, taken as their
    # authorization by the review itself, so every call it covers runs without asking. What
    # it never buys is an act nobody can undo, or one no review could look at.
    #
    # **A column rather than a reading of the empty prefix**, which would have been free.
    # An empty prefix is also what a scheduled task's pre-authorization seeds, and a task's
    # thread is an ordinary conversation the operator can reopen and carry on — so a width
    # inferred from emptiness, or from the kind of run asking, would silently promote every
    # one of those seeds the moment a human typed into the thread. Stored, a row says what
    # it authorizes without anyone having to know who is reading it.
    decisive: bool = Field(default=False)
    created_at: datetime = Field(default_factory=utcnow)
    # When the grant lapses back to strict per-call approval (UTC).
    expires_at: datetime
