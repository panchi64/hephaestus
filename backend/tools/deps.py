"""RunDeps, the per-run dependency object the agent hands to its tools.

It lives in ``tools/`` so both ``agent`` and ``tools`` import it without a cycle. It
becomes ``RunContext.deps``: a tool reaches the Run, the owner, the enabled-tool policy
and, through ``caps``, every capability handle, never via globals.

``caps`` is a typed container each feature manifest exports its tool-reachable services
into (``FeatureRuntime.capabilities``). A tool resolves a capability with
``ctx.deps.caps.get_optional(SomeStore)`` and returns an "unavailable" result when the
handle is absent; a new capability is one manifest export, never a new field here.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from pydantic_ai import RunContext

from core.container import ServiceContainer
from runs import Run
from services.agents.definition import UNRESTRICTED, ToolAccess
from services.home.paths import Place
from services.home.table import RunHome
from services.permissions import (
    DEFAULT_PERMISSION,
    PermissionLevel,
    ReviewBudget,
    TurnBoundary,
)
from services.search import DedupeSets
from services.workspace import RunWorkspace, scratch_key


def default_workspace_key(conversation_id: str | None, run: Run | None) -> str:
    """The key a run's workspace hangs off when nothing hands it one: the conversation,
    else the run itself (a stateless turn).

    Shared with the turn prelude, which stages attachments before any tool runs; two
    derivations drifting apart would stage files into the wrong workspace. Empty for a
    run-less ``RunDeps``, which only tests construct."""
    if run is None:
        return ""
    return conversation_id or run.id


#: The default for a ``disabled_tools`` parameter: nothing withheld. Immutable, so safe to share.
NO_DISABLED_TOOLS: frozenset[str] = frozenset()


@dataclass
class RunDeps:
    run: Run
    owner_id: str
    caps: ServiceContainer = field(default_factory=ServiceContainer)
    # Namespaced names withheld this run, by any reason in `services/tool_policy.py`'s union.
    disabled_tools: frozenset[str] = field(default_factory=frozenset)
    # The tools this run's agent is offered at all (`services/agents`).
    tool_access: ToolAccess = UNRESTRICTED
    # The furthest this run's agent may reach. A level raised mid-turn stays under it.
    permission_ceiling: PermissionLevel | None = None
    conversation_id: str | None = None
    # Resolved from the conversation's stored binding, never the live request: switching
    # the active project must not change a running thread.
    project_id: str | None = None
    # Mutable: `project_open_worktree` opens one mid-turn.
    worktree: bool = False
    # Whose write leases this run works under (`ConversationBinding.leases_from`).
    leases_from: str | None = None
    # Whether the checkout is read-only to this run because the thread whose leases it
    # holds works in a worktree (`ConversationBinding.checkout_read_only`).
    checkout_read_only: bool = False
    # Where a spawned agent's file changes are filed in the launcher's ledger. A run in its
    # own thread files under its own `run.prompt_message_id` (`tools/home.py`).
    ledger_prompt_message_id: str | None = None
    # "plan", "manual", "edit", "auto" or "yolo". Read by the toolset stack, which marks
    # every tool reaching past the level's write scope as needing approval before the
    # engine runs it (`services/permissions`).
    permission: PermissionLevel = DEFAULT_PERMISSION
    # A per-run cache memoised by `tools/workspace.py`, never set by a construction site.
    workspace: RunWorkspace | None = field(default=None, repr=False, compare=False)
    # Memoised by `tools/home.py` and replaced when a write lease widens it.
    home: RunHome | None = field(default=None, repr=False, compare=False)
    # Queries searched and pages fetched this run, so the web tools refuse a repeat. Per
    # run, not per conversation: "I already read this" is about the investigation in flight.
    web_dedupe: DedupeSets = field(default_factory=DedupeSets, repr=False, compare=False)
    # Whether another agent spawned this thread, so it is offered `subagent_report` and owes
    # its parent a report (`tools/report.py`).
    reports_to_parent: bool = False
    # Set once the report is filed, and once the run was sent back for ending without one,
    # so the reminder is given once.
    reported: bool = field(default=False, repr=False, compare=False)
    report_reminded: bool = field(default=False, repr=False, compare=False)
    # The projects and worktrees whose instruction files this run has handed over
    # (`tools/briefing.py`).
    briefed: set[Place] = field(default_factory=set[Place], repr=False, compare=False)
    # Keys the run's sandbox session, preview and staged artifacts. A field, not a derived
    # property, because a child workspace is a fork and delegation hands a sub-agent a
    # different one; empty at construction it fills itself in below.
    workspace_key: str = ""
    # A delegate whose work the operator approved as one act. Tools that ask the operator
    # have nobody to ask there (the conversation is the parent's) and are withheld.
    delegated_approved: bool = False
    # Where this turn's own messages begin and the turn's cap on model reviews. They are
    # here for the one ruling made inside a tool call, where the turn's locals cannot
    # reach: a call a `run_code` script made (`agent/code_mode.py`). The same objects as the
    # turn's, so a script cannot spend a review budget of its own. None on a run no turn built.
    turn_start: TurnBoundary | None = field(default=None, repr=False, compare=False)
    review_budget: ReviewBudget | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not self.workspace_key:
            self.workspace_key = default_workspace_key(self.conversation_id, self.run)

    @property
    def scratch_key(self) -> str:
        """The key of the scratch session this run works in: a sub-agent in a worktree of
        its own shares the scratch of the thread that spawned it."""
        return scratch_key(self.workspace_key)


# A manifest's `instructions` export, re-resolved every turn; returns "" when its capability
# is absent. Here so the engine and the feature layer name the shape without a cycle.
type AsyncInstructionProvider = Callable[[RunContext[RunDeps]], Awaitable[str]]
type InstructionProvider = Callable[[RunContext[RunDeps]], str] | AsyncInstructionProvider


@dataclass(frozen=True, slots=True)
class PromptContextRequest:
    """What a per-turn context block is resolved against.

    A struct so a new field is not a signature break across every manifest and sub-agent
    provider. It has no `RunContext`: these resolve before the agent starts.
    """

    caps: ServiceContainer
    owner_id: str
    conversation_id: str | None
    project_id: str | None
    #: Whether the turn works on its project in a worktree of its own.
    worktree: bool
    #: This run's workspace key — a delegation's own, where it has one, so a sub-agent
    #: is described its fork rather than its parent's files.
    workspace_key: str


# A manifest's `prompt_context` export. Delivered at the tail of the turn's user prompt, not
# the instructions head: volatile content at the head would invalidate the prompt-prefix
# cache from byte 0. Called before the agent starts, so it takes a `PromptContextRequest`.
type PromptContextProvider = Callable[[PromptContextRequest], Awaitable[str]]
