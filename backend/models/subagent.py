"""What links a sub-agent's transcript back to the thread that launched it.

The transcript itself needs no table. A sub-agent *is* a conversation — its messages are
ordinary ``Message`` rows, and an ephemeral conversation is hidden from the session list
but never reaped — so what it did is already durable. What is not durable without this row
is the **link**: which thread launched it, under what name, on what task, and how it ended.

Held only in memory, that link dies with the process, and the operator is left with a set
of unreachable conversations nothing can name. This row is what makes "show me what that
sub-agent did, and whether anything went wrong" answerable an hour later and a restart
later — which is the whole reason the transcripts are kept at all.

It is also the single source for three other readers that would otherwise each keep their
own count: the cap (how many are live), the parent's own wake (where to deliver a report),
and the panel (what to draw, live and finished). A second copy of any of those would be the
one that goes stale.

No ``__backup__``: like conversations themselves, a sub-agent's work is session history
rather than an operator preference, and it is exported — if at all — with the thread it
belongs to.
"""

from __future__ import annotations

from datetime import datetime
from typing import ClassVar

from sqlmodel import Field

from models._fields import new_id, utcnow
from models._table import Table


class SubagentRecord(Table, table=True):
    __tablename__: ClassVar[str] = "subagent_records"

    #: The same id that rides the wire as ``subagent_id`` and keys a card in the panel. Not
    #: shown to the *model*, which addresses a sub-agent by ``handle`` below.
    id: str = Field(default_factory=new_id, primary_key=True)
    owner_id: str = Field(index=True)
    #: The thread that launched this one — what the panel queries, and where a report is
    #: delivered when the sub-agent finishes. Indexed because every read is by parent.
    parent_conversation_id: str = Field(index=True)
    #: The thread at the top of the delegation tree, which bounds who may address whom.
    root_conversation_id: str = Field(default="", index=True)
    #: How far below the root thread this agent runs; its direct children are depth 1.
    depth: int = Field(default=1)
    #: The sub-agent's own (ephemeral) conversation: where its transcript lives.
    child_conversation_id: str = Field(index=True)
    #: Its Run — the live event stream while it works, and the approve endpoint while it
    #: is parked. Stale once the run is gone, which is what ``status`` is for.
    #:
    #: Indexed because *every* run in the process asks this table "was that one of mine?"
    #: at its terminal transition — the ordinary chat turns included — so an unindexed
    #: lookup is a full scan of every sub-agent ever launched, per run that ever ends.
    run_id: str = Field(index=True)
    #: The run that launched it, so a card ties back to the turn that asked.
    parent_run_id: str | None = Field(default=None)
    #: Which sub-agent this is — a roster name, built-in or project-declared. A plain
    #: string for the same reason a conversation's mode is one: a row written by another
    #: build, or naming a sub-agent a project has since deleted, must still load.
    spec_name: str
    #: The name the launching model gave *this* sub-agent — ``helper-function-finder`` for
    #: one of possibly three ``explorer``s — unique within the parent thread, and the only
    #: identifier the model is ever shown or ever addresses it by. The id above stays the
    #: primary key and the wire id; a uuid is the right thing for a database and the wrong
    #: thing to ask a model to carry across four tool calls and a fifty-thousand-token gap.
    #:
    #: Stored rather than derived because there is nothing to derive it from: it is a
    #: *choice* the launching turn made about this piece of work, and it has to outlive
    #: that turn — the panel labels a card with it, a report names it, and a parent
    #: addressing a sub-agent an hour later must land on the same one it launched.
    #: Indexed because every one of those reads is a lookup by (parent thread, handle).
    #:
    #: Rows written before handles existed were backfilled to their ``spec_name``, which is
    #: what the launcher falls back to anyway when a launch names no handle of its own — so
    #: an old card is addressable rather than merely legible.
    #:
    #: Not sealed, unlike the task beside it, and the difference is deliberate: this is
    #: looked *up* by, which an encrypted column cannot be, and the name rule holds it to a
    #: short identifier rather than a sentence. It is the one place a handle differs from
    #: the task it describes, and it is why ``helper-function-finder`` is a handle and "find
    #: the helper that normalises the customer's billing address" is not.
    handle: str = Field(default="", index=True)
    #: What it was asked to do. User content (the agent's words about the operator's
    #: work), so sealed like every other piece of it.
    task_enc: str
    #: ``running`` | ``blocked`` | ``done`` | ``failed`` | ``cancelled``.
    #:
    #: ``blocked`` is a sub-agent parked on an approval the operator has not answered: not
    #: finished, not progressing, and the one state where the thing it waits for is a
    #: person. A row still ``running`` at startup is reconciled to ``cancelled``, because
    #: the run that would have finished it died with the process.
    status: str = Field(default="running", index=True)
    #: Its report, sealed. Present on a sub-agent that finished; null on one that has not.
    summary_enc: str | None = Field(default=None)
    #: Why it failed, sealed for the same reason the report is — a failure message quotes
    #: the work as readily as a success does.
    error_enc: str | None = Field(default=None)
    #: How the sub-agent's workspace was taken, and the two names its teardown needs:
    #: a sandbox fork is merged and purged by ``workspace_key``, a worktree fork by
    #: (parent conversation, ``delegation_id``). Stored rather than re-derived, because an
    #: id that lives only in the call that asked for the fork is how a checkout is
    #: stranded — and that call is long over by the time the sub-agent ends.
    workspace_policy: str = Field(default="shared")
    workspace_key: str | None = Field(default=None)
    delegation_id: str | None = Field(default=None)
    #: The project a worktree fork belongs to: the merge runs from a terminal hook, long
    #: after the binding that knew this is gone.
    project_id: str | None = Field(default=None)
    #: The child's own context footprint, written when it settles. The *live* figure comes
    #: off the running Run's metrics; this is what a card shows for a sub-agent that
    #: finished before the page was opened, and it is deliberately not written on every
    #: progress frame — that would be a database write per token.
    context_used: int | None = Field(default=None)
    context_window: int | None = Field(default=None)
    started_at: datetime = Field(default_factory=utcnow)
    ended_at: datetime | None = Field(default=None)
