"""RunDeps — the per-run dependency object the agent hands to its tools.

Lives in ``tools/`` because it is the agent↔tools contract and ``tools`` sits
below ``agent`` in the dependency order (agent → tools → services → core), so
both layers import it without a cycle. It becomes ``RunContext.deps`` inside
Pydantic AI: a tool reaches the Run (to emit its own ``tool.progress`` events),
the owner, the per-run enabled-tool policy, and — through ``caps`` — every
capability handle, never via globals.

``caps`` is the **agent-facing capability bag**: a typed container each feature
manifest exports its tool-reachable services into (``FeatureRuntime.capabilities``),
assembled once at startup. A tool resolves its capability with
``ctx.deps.caps.get_optional(SomeStore)`` and **degrades** — returns an
"unavailable" result — when the handle is absent; a new capability is one export
on its own manifest, never a new field here or a new argument at any call site.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from pydantic_ai import RunContext

from core.container import ServiceContainer
from runs import Run
from services.agents.definition import UNRESTRICTED, ToolAccess
from services.permissions import (
    DEFAULT_PERMISSION,
    PermissionLevel,
    ReviewBudget,
    TurnBoundary,
)
from services.search import DedupeSets
from services.workspace import RunWorkspace
from services.workspace_kind import DEFAULT_WORKSPACE_KIND, WorkspaceKind


def default_workspace_key(conversation_id: str | None, run: Run | None) -> str:
    """The key a run's workspace hangs off when nothing hands it one: the conversation
    when there is one, else the run itself (a stateless turn).

    A function rather than a line written out at each site because the turn prelude
    resolves a workspace of its own — to stage attachments into — before any tool has
    run, and the two derivations drifting apart would stage the operator's files into a
    different workspace from the one the agent then works in. Empty for a run-less
    ``RunDeps``, which only tests construct."""
    if run is None:
        return ""
    return conversation_id or run.id


@dataclass
class RunDeps:
    run: Run
    owner_id: str
    # The capability handles this run's tools may reach (see the module docstring).
    # Empty ⇒ every capability-backed tool degrades — the same contract as a single
    # missing handle, applied uniformly.
    caps: ServiceContainer = field(default_factory=ServiceContainer)
    # Operator-disabled tools, by namespaced name. Empty ⇒ all enabled. Carries the
    # union of every reason a tool is withheld this run — the operator's own set,
    # offline mode's web suspension, and the rest of `services/tool_policy.py`'s union.
    disabled_tools: frozenset[str] = field(default_factory=frozenset)
    # The tools this run's agent is offered at all (`services/agents`).
    tool_access: ToolAccess = UNRESTRICTED
    # The furthest this run's agent may reach. A level raised mid-turn stays under it.
    permission_ceiling: PermissionLevel | None = None
    conversation_id: str | None = None
    # The project this run works in, or None for unfiled. Resolved from the
    # *conversation's* stored binding, never from the live request — switching the
    # active project must not change what an already-running thread is doing.
    project_id: str | None = None
    # Where this run's file work happens. Mutable: `project_open_worktree` moves a chat
    # from its sandbox to a worktree mid-turn, and the enabled gate reads it live.
    workspace_kind: WorkspaceKind = DEFAULT_WORKSPACE_KIND
    # How far this run may go before it has to ask: "plan", "manual", "edit", "auto" or
    # "yolo" — the last of which is the operator saying it never has to.
    # Here because a tool is offered and executed inside a Run,
    # long after the request that chose the level is gone — and read by the toolset stack,
    # which marks every tool reaching past the level's write scope as needing approval so
    # the engine sees the call before it runs (`services/permissions`).
    permission: PermissionLevel = DEFAULT_PERMISSION
    # This run's resolved workspace, memoised by `tools/workspace.py` on first use.
    # Never set by a construction site: it is a per-run cache, not an input, and it lives
    # here so it dies with the run instead of in a module-level dict.
    workspace: RunWorkspace | None = field(default=None, repr=False, compare=False)
    # The queries this run has already searched and the pages it has already fetched, so
    # the web tools can refuse a repeat before it costs a round trip. Per-run for the same
    # reason as `workspace` — and deliberately per *run* rather than per conversation:
    # "I already read this" is a fact about the investigation in flight, not a durable one.
    web_dedupe: DedupeSets = field(default_factory=DedupeSets, repr=False, compare=False)
    # The key everything about this run's workspace hangs off: its container session,
    # its preview, its staged artifacts. The conversation when there is one, else the
    # run (a stateless turn). A real field rather than a derived property because a
    # child workspace is a *fork* of its parent's, and delegation has to be able to
    # hand a sub-agent a different one; left empty at construction it fills itself in
    # below, so the ordinary case still needs no argument at any call site.
    workspace_key: str = ""
    # Whether this run is a delegate whose work the operator already approved as one act.
    # A tool whose whole job is to ask the operator something has nobody to ask inside such
    # a run — the conversation belongs to the parent — so it is withheld from one rather
    # than offered and left to strand the child on a question no one will see.
    delegated_approved: bool = False
    # The two things the Auto review needs that belong to the *turn* rather than to one
    # batch of deferred calls: where this turn's own messages begin, so the reviewer reads
    # the request that opened it, and the ceiling on model reviews the turn may spend.
    # `agent/turn.py` rules on a top-level batch with its own copies; these are here for
    # the one ruling that happens *inside* a tool call, where the turn's locals cannot
    # reach — a call a `run_code` script made (`agent/code_mode.py`). The same objects,
    # so a script cannot spend a review budget of its own. None on a run no turn built.
    turn_start: TurnBoundary | None = field(default=None, repr=False, compare=False)
    review_budget: ReviewBudget | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not self.workspace_key:
            self.workspace_key = default_workspace_key(self.conversation_id, self.run)


# A feature-contributed dynamic instruction (a manifest's `instructions` export): the
# engine registers each on the agent, it re-resolves fresh every turn, reaches its
# capability through the run's bag, and returns "" to no-op when the capability is
# absent. Part of the deps contract for the same reason `RunDeps` is — both the engine
# and the feature layer must name the shape without a cycle.
type InstructionProvider = Callable[[RunContext[RunDeps]], Awaitable[str]]


@dataclass(frozen=True, slots=True)
class PromptContextRequest:
    """What a per-turn context block is resolved against.

    A struct rather than a widening argument list because these providers are
    contributed by manifests and by both sub-agent paths, so every field added here
    would otherwise be a signature break across all of them. It carries the same
    handles a tool would reach the same facts through — the capability bag, and the
    three things that decide *which* workspace this turn is working in — but not a
    `RunContext`, because these resolve before the agent starts and there is no run to
    read them off.
    """

    caps: ServiceContainer
    owner_id: str
    conversation_id: str | None
    workspace_kind: WorkspaceKind
    project_id: str | None
    #: This run's workspace key — a delegation's own, where it has one, so a sub-agent
    #: is described its fork rather than its parent's files.
    workspace_key: str


# A feature-contributed per-turn context block (a manifest's `prompt_context` export):
# like an InstructionProvider it re-resolves fresh each turn and is never persisted, but
# the engine delivers it at the *tail* of the current turn's user prompt instead of the
# instructions block at the head of the request — volatile content at the head would
# invalidate the inference engine's prompt-prefix cache from byte 0 on every change,
# while a tail part leaves the whole history byte-stable. Called outside a live run
# (before the agent starts), so it takes a `PromptContextRequest` rather than a
# RunContext, and returns "" to no-op.
type PromptContextProvider = Callable[[PromptContextRequest], Awaitable[str]]
