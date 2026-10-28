"""Pillar II — the event protocol (the backend↔frontend contract).

The frozen v1 typed event union. Framing: SSE, each frame's ``id:`` is the
per-run monotonic ``seq`` and ``data:`` is the flat JSON envelope
``{type, seq, ts, ...payload}``. Naming is ``entity.event``, dot.lowercase —
past-tense verbs for things that happened, ``delta``/``progress`` for streams.

Producers build a typed body (e.g. ``AnswerDelta(text=...)``); the Run stamps
``seq``/``ts`` at emit time and wraps it in an :class:`Event`.
"""

from .approvals import (
    AnsweredQuestionOut,
    ApprovalDecided,
    ApprovalRequired,
    QuestionAnswered,
    QuestionAsked,
    QuestionOption,
    QuestionSpec,
    ReviewCompleted,
    ReviewStarted,
)
from .base import (
    PROTOCOL_VERSION,
    now_utc,
)
from .browser import (
    BrowserOpened,
)
from .citations import (
    CitationAdded,
)
from .context import (
    DEFAULT_CONTEXT_THRESHOLDS,
    INJECTED_TEXT_LIMIT,
    ContextComposition,
    ContextInjected,
    ContextSegment,
    ContextThresholds,
    ContextWindow,
    FoldPoint,
    InjectionPlacement,
)
from .conversation import (
    CompactionDelta,
    CompactionFailed,
    CompactionReason,
    CompactionStarted,
    ConversationCompacted,
    ConversationTitled,
)
from .lifecycle import (
    AnswerDelta,
    AnswerRetracted,
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
    InboxWaiting,
    MessageEdited,
    MessageHeld,
    MessageInjected,
    MessageQueued,
    MessageSource,
    MessageWithdrawn,
)
from .metrics import (
    LastRequestUsage,
    RunMetrics,
)
from .plan import (
    PermissionChanged,
    PlanUpdated,
    TasksUpdated,
)
from .plugins import (
    PluginWindowOpened,
)
from .tools import (
    ToolCompleted,
    ToolFailed,
    ToolImage,
    ToolPreparing,
    ToolProgress,
    ToolStarted,
)
from .union import (
    Event,
    EventBody,
)
from .view import (
    ViewLive,
    ViewLiveStopped,
    ViewSnapshot,
)

__all__ = [
    "AnswerDelta",
    "AnswerRetracted",
    "AnsweredQuestionOut",
    "ApprovalDecided",
    "ApprovalRequired",
    "BrowserOpened",
    "PluginWindowOpened",
    "CitationAdded",
    "CompactionDelta",
    "CompactionFailed",
    "CompactionReason",
    "CompactionStarted",
    "ContextComposition",
    "ContextInjected",
    "ContextSegment",
    "ContextThresholds",
    "ContextWindow",
    "ConversationCompacted",
    "ConversationTitled",
    "DEFAULT_CONTEXT_THRESHOLDS",
    "Event",
    "EventBody",
    "FoldPoint",
    "INJECTED_TEXT_LIMIT",
    "InboxWaiting",
    "InjectionPlacement",
    "LastRequestUsage",
    "LimitNotice",
    "MessageEdited",
    "MessageHeld",
    "MessageInjected",
    "MessageQueued",
    "MessageSource",
    "MessageWithdrawn",
    "PROTOCOL_VERSION",
    "PermissionChanged",
    "PlanUpdated",
    "QuestionAnswered",
    "QuestionAsked",
    "QuestionOption",
    "QuestionSpec",
    "ReviewCompleted",
    "ReviewStarted",
    "RunEnded",
    "RunError",
    "RunMetrics",
    "RunQueued",
    "RunStarted",
    "StepCompleted",
    "StepStarted",
    "TasksUpdated",
    "ThinkingDelta",
    "ToolCompleted",
    "ToolFailed",
    "ToolImage",
    "ToolPreparing",
    "ToolProgress",
    "ToolStarted",
    "ViewLive",
    "ViewLiveStopped",
    "ViewSnapshot",
    "now_utc",
]
