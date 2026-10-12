"""Conversation persistence schema.

A conversation owns a **tree** of messages, not a flat list. Each message stores
**both** a serialized Pydantic AI ``ModelMessage`` blob (full fidelity, so a cold
session rehydrates exactly) **and** a thin projection (kind + text) for listing
and search. The projection is derived, never authoritative.

The tree is what makes regenerate / edit / rewind possible. Every message points
at its predecessor via ``parent_id``; **messages sharing a parent are alternative
continuations — versions.** Regenerating an answer adds a sibling under the same
user request; editing a user turn adds a sibling under the same parent. The
conversation's ``active_leaf_id`` is the tip of the path the operator is currently
viewing — walking it parent-by-parent to the root yields the active history, the
flat list the agent actually runs against. ``seq`` is a per-conversation,
monotonically increasing creation counter; it no longer implies linear order, but
it gives siblings a stable version ordering (oldest first) and keeps writes
collision-free across branches.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Column, UniqueConstraint
from sqlmodel import Field, SQLModel

from models._fields import new_id, utcnow


class Conversation(SQLModel, table=True):
    __tablename__ = "conversations"

    id: str = Field(default_factory=new_id, primary_key=True)
    owner_id: str = Field(index=True)
    # The project this belongs to, or null for **unfiled** — visible in every scope,
    # not orphaned. See models/project.py for the one scope rule.
    project_id: str | None = Field(default=None, index=True)
    # Where its file work happens: "sandbox", "checkout" or "worktree"
    # (`services/workspace_kind.py`). A plain string so a restored backup or a row from
    # another build still loads; an unrecognised value reads as the sandbox. Moves one way
    # only, checkout to worktree, when the agent opens one: a worktree thread owns a
    # branch, and moving it back would strand that branch.
    workspace_kind: str = Field(default="sandbox")
    # The agent this thread runs (`services/agents`), by name. Null is the default agent.
    # A name that no longer resolves runs the default agent too.
    agent_name: str | None = Field(default=None)
    # How much rope the model gets in this thread — "plan", "manual", "edit", "auto" or
    # "yolo".
    # What each permits is the permission registry's answer (`services/permissions`); the
    # column stores only the name, as a plain string for the same reason as `workspace_kind`.
    # **This moves freely.** It is the operator's live control over a thread
    # already in flight: a Plan thread that produced a plan is raised to Edit and carries
    # on in place, and a thread that is about to touch something delicate is dropped to
    # Manual mid-conversation. Nothing structural hangs off it — no branch, no workspace —
    # so re-pointing it strands nothing. Policy, not user content, so it stays in the
    # clear like `model`/`workspace_kind`/`ephemeral`.
    # **The Python default only** — deliberately not a `server_default` change, so no
    # migration re-points a thread the operator already set. Existing rows keep whatever
    # they were stored with; a row written from here on starts where a fresh thread starts.
    permission_level: str = Field(default="auto")
    # AEAD ciphertext of the thread's title. It is user content — an auto-generated
    # summary of the operator's own first message, and the most revealing single line a
    # thread has — so it is sealed like every peer entity's title (XC-SEC-3). Null for an
    # untitled thread.
    title_enc: str | None = None
    # The pre-encryption cleartext title. Kept **only** for rows written before the title
    # was sealed: reads prefer `title_enc` and fall back here, and the startup backfill
    # seals each remaining value and nulls this out (services/sealing.py). Nothing writes
    # it any more, so it is null on every new row and drains to null on every old one — a
    # migration can't do the sealing itself, because it runs before unlock with no key.
    title: str | None = None
    # AEAD ciphertext of the thread's **work summary** — two or three sentences saying what
    # was asked, what the agent actually did, and where things stand, written by a
    # background sweep once the thread has been idle a while and shown in the re-entry band
    # when the operator comes back to it. It is user content of the most concentrated kind
    # (it names the files, commands and sources a thread touched), so it is sealed exactly
    # as the title above is. **No cleartext twin**: the column is new, so nothing was ever
    # written here unsealed and `services/sealing.py` has nothing to backfill.
    #
    # A single value, replaced whole on every regeneration rather than appended to — the
    # band shows one account of the thread, and a history of superseded ones would be a
    # second thing to seal, to bound and to decide staleness for.
    work_summary_enc: str | None = None
    # When the summary above was generated. It is the whole of the staleness rule: a summary
    # is stale when this is null or older than `updated_at`, so a thread that has spoken
    # since it was last summarised is a candidate again and one that has not is skipped for
    # free. A timestamp rather than a message id or a hash because the sweeper's other
    # condition — "idle for N minutes" — already reads `updated_at`, and comparing two
    # columns of the same kind is one query rather than a load per thread.
    work_summary_at: datetime | None = None
    # Tip of the path the operator is currently viewing. Walking it parent-by-parent
    # to the root is the active history. Null only for an empty conversation; a
    # cold load that finds it dangling falls back to the deepest leaf by seq.
    active_leaf_id: str | None = None
    # The model the active path last ran on (its most recent answer's model_name),
    # denormalized so the listing reads it without opening a message blob. Kept in
    # step with active_leaf_id by the write-behind store; structural metadata, not
    # user content, so it stays in the clear. Null until the first answer.
    model: str | None = None
    # A scratch conversation that the listing hides — used by the side-by-side
    # compare surface, where each pane is a throwaway thread the operator never
    # meant to keep. Still a fully real conversation (readable, resumable, branch-
    # able by id); it's only omitted from the conversation list and the count.
    ephemeral: bool = Field(default=False)
    # Per-conversation override of the global conversation auto-compaction toggle (folding
    # older turns into a summary the thread's own model writes once the context footprint
    # nears the model's window): null inherits the operator default, True/False forces it
    # on/off for this thread. Policy, not user content, so it stays in the clear — like
    # `model`/`ephemeral`.
    auto_compact_override: bool | None = Field(default=None)
    # What the most recent turn's model request weighed besides the conversation itself:
    # the standing brief and the tool schemas, itemised, in characters (the serialized
    # form of `runs.TurnOverhead`). The context readout splits the thread's footprint
    # with it on a cold load, where there is no live request to measure — the schemas and
    # the brief never reach the message history, so without this a reopened thread could
    # only report one undifferentiated "in use" figure until the operator sent another
    # message, which is exactly when they least want to (the reason to look is to decide
    # whether to send one at all).
    #
    # **Per conversation, last turn wins.** The figure it splits is itself the last
    # turn's — `context_footprint` reads the newest response's usage — so both halves of
    # that readout come from the same turn and describe the same request. Per-message
    # would follow a rewind exactly, at the cost of a near-identical blob on every
    # response row of every agentic step; what that trades for is drift in one narrow
    # case (an older turn's footprint split by a newer turn's configuration, after tools
    # were toggled mid-thread), and the drift moves proportions only. The total stays the
    # provider's own number, and every figure derived from this renders with a `~`.
    #
    # Slugs and character counts — structural metadata, not user content — so it stays in
    # the clear like `model`/`workspace_kind`/`ephemeral`. Null until the thread's first turn.
    context_overhead: dict | None = Field(default=None, sa_column=Column(JSON, nullable=True))
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class Message(SQLModel, table=True):
    __tablename__ = "messages"
    # The creation counter is unique per conversation — a double-insert (e.g. a
    # retried write that partly landed) fails loudly instead of silently
    # duplicating. seq no longer implies linear order (the tree does), only the
    # order in which rows were created.
    __table_args__ = (UniqueConstraint("conversation_id", "seq", name="uq_message_conv_seq"),)

    id: str = Field(default_factory=new_id, primary_key=True)
    conversation_id: str = Field(index=True, foreign_key="conversations.id")
    # The message this one follows. Null = a root (the conversation's first
    # message). Messages sharing a parent are alternative continuations = versions.
    parent_id: str | None = Field(default=None, index=True, foreign_key="messages.id")
    seq: int = Field(index=True)  # monotonic creation counter, from 0; orders siblings
    kind: str  # "request" | "response"
    # Operator's pin on this turn — a durable bookmark surfaced in the projection.
    # Set on the turn's branch node (the user request, or an assistant's first response).
    pinned: bool = Field(default=False)
    text: str  # projection for listing/search
    blob: str  # one serialized ModelMessage (JSON) for resume fidelity
    # Upload ids the operator attached to this turn (a user request). The blob carries
    # only a compact marker for them — never the file bytes/text — so the file is not
    # replayed into context on every later turn (the agent pulls it from the corpus on
    # demand); these ids are the durable link the UI renders as chips and a regenerate
    # re-resolves to re-supply the file for the active turn. Opaque ids, so in the clear.
    attachment_ids: list[str] = Field(sa_column=Column(JSON, nullable=False, default=list))
    # Set on an assistant turn's branch node when the run backing it ended
    # `outcome: "blocked"` (a usage/loop/context/time bound, not a normal finish) —
    # the human-readable reason (`Run.detail`), so a reload shows the same
    # persistent stop marker the live stream rendered. Null for every other turn.
    blocked_reason: str | None = None
    # A conversation-compaction checkpoint: this node's blob is a summary, written by the
    # thread's own model, of everything on its path up to and including
    # `compacted_through`, folded in once the context footprint neared the model's window.
    # Nothing is deleted — the summarized turns stay in the tree and in the operator's
    # transcript; the checkpoint only changes what the *model* replays (see
    # `ConversationStore.model_history`). A checkpoint row deliberately carries an empty
    # `text`, so it contributes no embedding, no cross-chat search hit, and no listing
    # preview — the summary lives only in the sealed blob.
    compacted: bool = Field(default=False)
    compacted_through: str | None = None
    # What this model response cost in wall-clock, measured by us around our own
    # streaming (never read off a provider, so it means the same thing on every
    # endpoint — see runs/timings.py). Null on request rows, and on responses written
    # before this was recorded; the readout sums whatever is present and reports the
    # rest as unmeasured. Structural metadata, not user content, so in the clear.
    #
    # On the *message* rather than in a per-conversation total because the thread is a
    # tree: a rewind or a version switch changes which responses are on the active
    # path, and a running total would keep charging the operator for a branch they
    # left. Every other figure in the readout is already derived from the path this
    # way — time is only here because it isn't recoverable from the message blob.
    llm_ms: int | None = None  # full model round-trip: connect, queue, generate, stream
    ttft_ms: int | None = None  # to the first content part of any kind, reasoning included
    tool_ms: int | None = None  # executing the tool calls this response asked for
    # Semantic-search vector over `text`, encrypted at rest like the projection it
    # embeds. Null when the message has no searchable text (tool/reasoning-only
    # turns) or the embedder was unavailable when it was persisted — such a message
    # falls back to keyword-only recall, the same degrade memories use (EMB-2). The
    # model/dim record the embedding space so vectors are only ever compared within it.
    embedding_enc: str | None = None
    embedding_model: str | None = None
    embedding_dim: int | None = None
    created_at: datetime = Field(default_factory=utcnow)
