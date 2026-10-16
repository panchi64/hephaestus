"""The store's public value types: a thread's binding, a turn's stamps, a compaction plan and the
listing summary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from pydantic_ai import ModelMessage

from services.agents.definition import UNRESTRICTED, ToolAccess
from services.commands.spec import Invocation
from services.permissions import DEFAULT_PERMISSION, PermissionLevel


@dataclass(frozen=True)
class ConversationBinding:
    """How a thread works: the project it is filed under, and how far the model may go on
    its own.

    Read as one record because the parts are only meaningful together: a worktree with no
    project is nonsense, and a run that resolved its project from the conversation but
    its permission level from somewhere else would be two threads wearing one name.
    """

    project_id: str | None = None
    #: Whether the thread works on its project in a worktree of its own.
    worktree: bool = False
    #: Whose write leases the thread works under, and only reads: a spawned agent sharing
    #: its parent's files holds the parent's, one working apart holds none (``""``). None
    #: is the thread's own, which it may add to.
    leases_from: str | None = None
    permission: PermissionLevel = DEFAULT_PERMISSION
    #: The agent the thread runs, by name. None is the default agent.
    agent_name: str | None = None
    #: The tools that agent is offered. Not stored: applied from the agent's definition
    #: by whoever resolves it for a turn.
    tools: ToolAccess = UNRESTRICTED
    #: The furthest that agent may reach, which a level raised mid-turn stays under.
    permission_ceiling: PermissionLevel | None = None
    #: The workspace its turns work in. Empty is the thread's own.
    workspace_key: str = ""


@dataclass(frozen=True)
class TurnStamps:
    """What a turn's user request carries besides the operator's words.

    Both fields ride the request's ``metadata`` rather than columns of their own (see
    ``services/conversation_view``), and both exist for the same reason: a turn can be
    **re-run** — regenerated, or edited and re-asked — and everything that composed it the
    first time has to be recoverable from what was written down, not from a client that may
    have been closed since.

    Read as one record because both callers want both, and one round trip through the tree
    is what they cost together.
    """

    command: Invocation | None = None
    file_refs: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class CompactionPlan:
    """What one conversation compaction would fold, resolved against the active path.

    ``messages`` is the model's *current* replay view up to the boundary — so a second
    compaction summarizes the first summary plus what followed it, never the original
    turns all over again. ``expected_leaf_id`` is re-checked at record time, because
    generating the summary takes seconds and the operator can switch versions meanwhile."""

    messages: list[ModelMessage]
    through_id: str
    expected_leaf_id: str
    # The rendered turn the divider follows — what a live client needs to place it where a
    # reload will, since it addresses turns, not tree nodes. None when the covered node
    # doesn't resolve to a turn (defensive; the client then appends).
    anchor_id: str | None


@dataclass
class ConversationSummaryView:
    """A listing projection — never the authoritative history, just enough to
    render a sidebar row."""

    id: str
    title: str | None
    created_at: datetime
    updated_at: datetime
    message_count: int
    preview: str | None
    # The idle thread's work summary — what the agent did here, in two or three sentences,
    # written by the background sweep (`services/work_summaries.py`). None until a thread
    # has been idle long enough to have earned one.
    #
    # On the *listing* projection, beside `preview`, because that is where the frontend's
    # re-entry band reads it from: the band is drawn over a row the session list already
    # loaded, and a short summary is the same class of payload as the excerpt sitting next
    # to it. A second fetch per row would be the same bytes over more round trips.
    work_summary: str | None = None
    # The model the conversation last ran on (the most recent response's
    # model_name). None for a conversation with no answer yet.
    model: str | None = None
    # The project the thread is filed under, or None for an unfiled thread. The id, not the
    # path — the route resolves it to a directory basename, which is the only part of a
    # host path a listing should ever carry.
    project_id: str | None = None
    agent_name: str | None = None
    #: The thread that spawned this one, for an opened agent thread.
    parent_id: str | None = None
