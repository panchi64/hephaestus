"""What links a sub-agent's transcript back to the thread that launched it.

A sub-agent is a conversation: its messages are ordinary ``Message`` rows in an ephemeral
conversation that is hidden but never reaped, so the transcript is already durable. This
row holds the link: which thread launched it, under what name, on what task, and how it
ended. Kept in memory it would die with the process, leaving unreachable conversations.

It is also the single source for the cap (how many are live), the parent's wake (where to
deliver a report) and the panel, so no copy can go stale.

No ``__backup__``: a sub-agent's work is session history, exported if at all with its thread.
"""

from __future__ import annotations

from datetime import datetime
from typing import ClassVar

from sqlmodel import Field

from models._fields import new_id, utcnow
from models._table import Table


class SubagentRecord(Table, table=True):
    __tablename__: ClassVar[str] = "subagent_records"

    #: Rides the wire as ``subagent_id`` and keys a panel card. The model addresses a
    #: sub-agent by ``handle`` instead.
    id: str = Field(default_factory=new_id, primary_key=True)
    owner_id: str = Field(index=True)
    #: The launching thread: what the panel queries and where a report is delivered. Indexed
    #: because every read is by parent.
    parent_conversation_id: str = Field(index=True)
    #: The thread at the top of the delegation tree, which bounds who may address whom.
    root_conversation_id: str = Field(default="", index=True)
    #: How far below the root thread this agent runs; its direct children are depth 1.
    depth: int = Field(default=1)
    #: The sub-agent's own (ephemeral) conversation, where its transcript lives.
    child_conversation_id: str = Field(index=True)
    #: Its Run: the live event stream while it works and the approve endpoint while parked.
    #: Stale once the run is gone (see ``status``).
    #:
    #: Indexed because every run in the process, chat turns included, asks this table "was
    #: that one of mine?" at its terminal transition; unindexed that is a full scan per run.
    run_id: str = Field(index=True)
    #: The run that launched it, so a card ties back to the asking turn.
    parent_run_id: str | None = Field(default=None)
    #: A roster name, built-in or project-declared. A plain string so a row from another
    #: build, or naming a since-deleted project sub-agent, still loads.
    spec_name: str
    #: The name the launching model gave this sub-agent (``helper-function-finder`` for one
    #: of several ``explorer``s), unique within the parent thread and the only identifier the
    #: model is shown or addresses it by; a uuid is wrong to ask a model to carry across many
    #: tool calls. The id stays the primary key and wire id.
    #:
    #: Stored because it is a choice the launching turn made, and the panel label, a report
    #: and a parent addressing it an hour later must all land on the same sub-agent. Indexed
    #: for lookup by (parent thread, handle). Rows predating handles were backfilled to
    #: ``spec_name``, the launcher's fallback when a launch names none.
    #:
    #: Not sealed, unlike the task: it is looked up by, which an encrypted column can't
    #: support, and the name rule holds it to a short identifier rather than a sentence.
    handle: str = Field(default="", index=True)
    #: Its class's key in ``prompts.voice.CLASSES`` and the callsign drawn from that class's
    #: pool. Empty on rows written before either.
    agent_class: str = Field(default="")
    callsign: str = Field(default="")
    #: What it was asked to do. User content, so sealed.
    task_enc: str
    #: ``running`` | ``blocked`` | ``done`` | ``failed`` | ``cancelled``.
    #:
    #: ``blocked`` is parked on an approval the operator has not answered. A row still
    #: ``running`` at startup is reconciled to ``cancelled``: its run died with the process.
    status: str = Field(default="running", index=True)
    #: Its report, sealed; null until it finishes.
    summary_enc: str | None = Field(default=None)
    #: Why it failed, sealed like the report (a failure message quotes the work too).
    error_enc: str | None = Field(default=None)
    #: Where the sub-agent works, and the names its worktree is found by: (the owner half
    #: of ``workspace_key``, ``delegation_id``). Stored because the parent merges or
    #: discards that worktree long after the launch that cut it.
    workspace_policy: str = Field(default="shared")
    workspace_key: str | None = Field(default=None)
    delegation_id: str | None = Field(default=None)
    project_id: str | None = Field(default=None)
    #: The commit the sub-agent's worktree was cut at, so a merge carries only its own work.
    base_ref: str | None = Field(default=None)
    #: The launching turn's prompt, under which this agent's file changes are filed in the
    #: launcher's ledger; stored because a later wake still files there.
    ledger_prompt_message_id: str | None = Field(default=None)
    #: The child's context footprint, written when it settles, for a card of a sub-agent that
    #: finished before the page opened. The live figure comes from the Run's metrics; not
    #: written per progress frame, which would be a database write per token.
    context_used: int | None = Field(default=None)
    context_window: int | None = Field(default=None)
    started_at: datetime = Field(default_factory=utcnow)
    ended_at: datetime | None = Field(default=None)
