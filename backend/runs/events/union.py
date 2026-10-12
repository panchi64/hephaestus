"""The assembled event union and the stamped :class:`Event` envelope."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any

from pydantic import BaseModel, Field

from .approvals import (
    ApprovalRequired,
    QuestionAnswered,
    QuestionAsked,
    ReviewCompleted,
    ReviewStarted,
)
from .citations import CitationAdded
from .context import ContextInjected
from .conversation import (
    CompactionDelta,
    CompactionFailed,
    CompactionStarted,
    ConversationCompacted,
    ConversationTitled,
)
from .lifecycle import (
    AnswerDelta,
    LimitNotice,
    RunEnded,
    RunError,
    RunQueued,
    RunStarted,
    StepCompleted,
    StepStarted,
    ThinkingDelta,
)
from .messages import (
    MessageEdited,
    MessageHeld,
    MessageInjected,
    MessageQueued,
    MessageWithdrawn,
)
from .metrics import RunMetrics
from .plan import PermissionChanged, PlanUpdated, TasksUpdated
from .tools import ToolCompleted, ToolFailed, ToolProgress, ToolStarted
from .view import ViewLive, ViewLiveStopped, ViewSnapshot

#: The ``subagent.*`` family that used to live here is gone with the blocking delegation
#: that emitted it. It existed because a delegation was a multi-minute silence *inside*
#: one tool call, so the parent's own stream had to narrate a child it was holding open.
#: A sub-agent is now a run of its own: it has a stream, a transcript and a register row,
#: and the panel reads those directly. Relaying a second copy of them onto whichever
#: parent run happened to be open would be a feed that stops the moment the parent's turn
#: ends — which is most of a sub-agent's life.


EventBody = Annotated[
    RunStarted
    | RunQueued
    | RunMetrics
    | RunEnded
    | RunError
    | StepStarted
    | StepCompleted
    | ThinkingDelta
    | AnswerDelta
    | ToolStarted
    | ToolProgress
    | ToolCompleted
    | ToolFailed
    | CitationAdded
    | ViewLive
    | ViewLiveStopped
    | ViewSnapshot
    | ConversationTitled
    | CompactionStarted
    | CompactionDelta
    | CompactionFailed
    | ConversationCompacted
    | ContextInjected
    | ApprovalRequired
    | QuestionAsked
    | QuestionAnswered
    | ReviewStarted
    | ReviewCompleted
    | MessageQueued
    | MessageEdited
    | MessageWithdrawn
    | MessageHeld
    | MessageInjected
    | TasksUpdated
    | PlanUpdated
    | PermissionChanged
    | LimitNotice,
    Field(discriminator="type"),
]


@dataclass(frozen=True, slots=True)
class Event:
    """A stamped event: the producer's body plus the run-assigned seq/ts."""

    seq: int
    ts: datetime
    body: BaseModel

    def envelope(self) -> dict[str, Any]:
        """Flat ``{type, seq, ts, ...payload}`` dict — the on-the-wire shape."""
        data = self.body.model_dump(mode="json")
        data["seq"] = self.seq
        data["ts"] = self.ts.isoformat()
        return data

    def sse(self) -> str:
        """One SSE frame: ``id:`` carries seq for Last-Event-ID resume."""
        payload = json.dumps(self.envelope(), separators=(",", ":"))
        return f"id: {self.seq}\ndata: {payload}\n\n"
