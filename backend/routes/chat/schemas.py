"""Wire shapes of the chat surface: the send / regenerate / edit bodies, the created-run
reply, and the operator's chat preferences."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from services.commands import Invocation
from services.modes import DEFAULT_MODE, ModeId
from services.permissions import PermissionLevel
from services.settings_store import WORK_SUMMARY_IDLE_MINUTES_MAX


class CommandInvocation(BaseModel):
    """Which slash command was picked, and what the operator typed after it."""

    #: A bare name (``reviewer``) or a qualified one (``agent:reviewer``). Resolved against
    #: the same narrowed catalog the picker was served, so a command this thread is not
    #: offered cannot be reached by typing its name.
    name: str
    argument: str = ""

    def recorded(self) -> Invocation:
        """The wire shape as the fact that gets written onto the turn.

        Two near-identical two-field records, kept apart on purpose: this one is a
        client's claim and is validated as one, while :class:`~services.commands.Invocation`
        is what the store stamps and what a regenerate reads back. Collapsing them would
        put a Pydantic model from a route into the conversation store's signature — and
        into the sealed blob's metadata — which is the wrong direction for a dependency
        that already runs the other way.
        """
        return Invocation(name=self.name, argument=self.argument)


class ChatCreate(BaseModel):
    prompt: str
    conversation_id: str | None = None  # continue an existing conversation
    # Per-conversation `main` override from the chat picker: which provider
    # (`endpoint_id`) and which model on it (`model`, discovered from the provider).
    endpoint_id: str | None = None
    model: str | None = None
    # Files the operator attached to this message (existing upload ids — the client
    # uploads first via POST /uploads, then sends the ids here). They're handed to the
    # model for this turn and enrolled in the knowledge base via the upload pipeline.
    attachment_ids: list[str] = []
    # Files the operator named with `@` in the composer, relative to the run's workspace.
    # A **reference only**: the turn names the paths and the model reads what it wants
    # with the file tools. Nothing here is read on the way in, so a reference cannot go
    # stale in history the way a pasted copy would.
    file_refs: list[str] = []
    # When creating a fresh conversation, mark it a scratch thread the listing
    # hides (the side-by-side compare panes set this). Ignored when continuing an
    # existing conversation.
    ephemeral: bool = False
    # What kind of work a *new* thread is — the vocabulary and everything each mode
    # implies live in the registry (`services/modes.py`). "normal" and "research" run in
    # the conversation's own sandbox container; "code" runs in a git worktree of
    # `project_id`'s repository on the host. Both this and `project_id` are ignored when
    # continuing an existing conversation — the binding is set once, at creation, and a
    # thread's branch would be stranded if it could move.
    #
    # Deliberately **not** `code_mode`: `pydantic_ai_harness` ships a capability called
    # `CodeMode`, and it is an unrelated thing (a context-saving trick where the model
    # writes one script that calls many tools). This is a mode of the chat.
    mode: ModeId = DEFAULT_MODE
    project_id: str | None = None
    # How far the model may go this turn — and, unlike `mode`, from this turn onwards:
    # the level is persisted on the thread, so switching mid-conversation is a plain send
    # rather than a separate call, and a reload comes back at the level the operator left
    # it. Absent means "whatever the thread is already at" (a new thread starts at its
    # mode's default), so a client that never learned about levels keeps working.
    permission_level: PermissionLevel | None = None
    # Set when this turn is the operator resuming a turn a bound stopped (the
    # "Continue" button under a stop marker): the branch node id of the turn that
    # carries the marker. Accepting the turn retires that marker durably, so the
    # warning doesn't outlive the thing it was asking for. Ignored on a new
    # conversation — nothing there can be blocked yet.
    continues_message_id: str | None = None
    # A slash command the operator picked in the composer. The prompt above is still their
    # literal `/review the auth route` — this says which command that token was, so the
    # backend can resolve it and expand it. Deliberately *not* a pre-expanded prompt: what
    # a command means is the backend's to decide, and a client that sent the expansion
    # would be sending a turn the operator never wrote.
    command: CommandInvocation | None = None


class RegenerateCreate(BaseModel):
    conversation_id: str
    message_id: str  # the assistant turn to re-answer (its branch node id)
    # Optional per-turn model override — regenerate with a different provider/model.
    endpoint_id: str | None = None
    model: str | None = None


class EditCreate(BaseModel):
    conversation_id: str
    message_id: str  # the user turn to replace (its request node id)
    prompt: str  # the edited message
    endpoint_id: str | None = None
    model: str | None = None
    # Attachments for the edited turn — a fresh user request, so the same direct-inject
    # path as a new message (regenerate, which adds no new request, takes none).
    attachment_ids: list[str] = []


class ChatCreated(BaseModel):
    run_id: str
    conversation_id: str
    # Set when the conversation already had a live run and this message was queued
    # into it (to be injected at the run's next model-request boundary) instead of
    # starting a new run. Additive — a plain send never carries it.
    queued_message_id: str | None = None


class ChatSettings(BaseModel):
    """Operator-tunable chat preferences. The ``auto_compact*`` fields tune conversation
    compaction — the product's one context reduction: fold older *turns* into a
    summary the thread's own model writes once the context window fills.
    ``agent_request_limit`` is how many model round-trips one turn may spend before it
    stops. They're optional on a PUT — an omitted one is left unchanged — and always
    populated on a GET. snake_case out, matching the rest of the ``/chat`` surface.

    On a PUT, ``wall_clock_timeout_s`` is the one field where ``null`` is a *value* (no
    bound) rather than an omission, so the handler reads ``model_fields_set`` for it
    instead of testing against ``None`` like the rest. On a GET, ``null`` is meaningful in
    two fields, not one: an absent wall clock, and an ``inactivity_timeout_s`` a deploy
    disabled outright."""

    # `extra="forbid"` so a mistyped/unknown field is a 422, not a silent no-op: with every
    # field optional (omitted ⇒ unchanged), a typo'd key would otherwise be dropped and the PUT
    # would return 200 having changed nothing.
    model_config = ConfigDict(extra="forbid")

    # All fields are optional on a PUT (an omitted one is left unchanged) and always
    # populated on a GET; the bounds still reject a nonsensical value when one is provided.
    # Conversation auto-compaction: whether to fold older turns into a summary, and how
    # full the model's context window must get first. A **fraction**, not a percentage —
    # the same 0–1 quantity the context meter reports, so the client formats one number
    # rather than the wire carrying two conventions. Bounded above 0 (a 0 threshold would
    # fire on an empty thread) and at 1 (above it, compaction could never fire at all).
    auto_compact_enabled: bool | None = None
    auto_compact_threshold: float | None = Field(default=None, gt=0, le=1)
    # How many minutes a thread must be quiet before the background sweep writes its work
    # summary — the account of what the agent did that the re-entry band shows when the
    # operator comes back to it. ``ge=1``: a zero-minute window is not a faster setting but
    # a broken one, since every thread would qualify the instant its turn finished and the
    # sweep would be summarizing conversations that are still going. The ceiling keeps the
    # window inside a day, past which the band would never appear on a thread worked on in
    # a normal week — which is indistinguishable from the feature being switched off.
    work_summary_idle_minutes: int | None = Field(
        default=None, ge=1, le=WORK_SUMMARY_IDLE_MINUTES_MAX
    )
    # The context gauge's severity boundaries — the fullness at which the ring under the
    # composer turns amber, then red. Fractions, like the compaction threshold above and
    # for the same reason. The per-field bounds here only reject a value that is
    # nonsensical on its own; the pair's ordering (`warn` below `alert`) is checked
    # against the *merged* values in the PUT, since either may be omitted.
    context_warn_threshold: float | None = Field(default=None, gt=0, lt=1)
    context_alert_threshold: float | None = Field(default=None, gt=0, le=1)
    # ``ge=1``, not ``ge=0``: a turn allowed zero model requests could never produce an
    # answer, so 0 is a nonsensical value to accept rather than merely a minimal one.
    agent_request_limit: int | None = Field(default=None, ge=1)
    # The inactivity watchdog's bound in seconds: how long a run may go without emitting
    # an event before it is stopped. ``gt=0`` (a 0 bound would stop every turn
    # immediately); the config default applies when the operator hasn't set one, and a
    # deploy-level ``None`` disables the watchdog entirely.
    inactivity_timeout_s: float | None = Field(default=None, gt=0)
    # The wall-clock bound in seconds: how long a run may take in total, however busy it
    # is. Off unless the operator sets one — a turn is already bounded by
    # `agent_request_limit`, so a wall clock mostly stops runs that are merely slow. What
    # it does catch is a run that keeps emitting (a tool streaming progress, a model
    # streaming tokens) and so refreshes the inactivity watchdog forever without spending
    # a model request. ``gt=0`` for the same reason as above; ``null`` removes the bound.
    wall_clock_timeout_s: float | None = Field(default=None, gt=0)
    # How many sub-agents the agent may have running at once. ``null`` is *no cap*, which
    # is the default — so this is the second field where ``null`` is a value rather than an
    # omission, and the handler reads ``model_fields_set`` for it too. ``ge=1``: a cap of 0
    # would be a way of disabling the feature through a number, and the tool catalog
    # already disables it honestly.
    subagent_max_concurrent: int | None = Field(default=None, ge=1)
