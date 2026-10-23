"""Wire shapes of the chat surface: send, regenerate and edit bodies, the created-run reply
and chat preferences."""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

from services.commands import Invocation
from services.permissions import PermissionLevel
from services.settings_store import WORK_SUMMARY_IDLE_MINUTES_MAX


class CommandInvocation(BaseModel):
    """Which slash command was picked, and what the operator typed after it."""

    #: A bare name (``reviewer``) or qualified (``agent:reviewer``), resolved against the
    #: narrowed catalog the picker was served, so an unoffered command can't be typed in.
    name: str
    argument: str = ""

    def recorded(self) -> Invocation:
        """The wire shape as the fact written onto the turn.

        Kept apart from :class:`~services.commands.Invocation` (what the store stamps and a
        regenerate reads back): merging them would put a route's Pydantic model into the
        conversation store's signature and the sealed blob's metadata, against the
        dependency direction.
        """
        return Invocation(name=self.name, argument=self.argument)


class ChatCreate(BaseModel):
    prompt: str
    conversation_id: str | None = None  # continue an existing conversation
    # Per-conversation `main` override from the picker: the provider and a model on it.
    endpoint_id: str | None = None
    model: str | None = None
    # Existing upload ids (the client uploads first via POST /uploads). They go to the
    # model for this turn and are enrolled in the knowledge base.
    attachment_ids: list[str] = []
    # Paths named with `@`, relative to the run's workspace. A reference only: the model
    # reads what it wants, so a reference can't go stale in history like a pasted copy.
    file_refs: list[str] = []
    # Marks a fresh conversation as a scratch thread the listing hides (compare panes);
    # ignored when continuing one.
    ephemeral: bool = False
    # The project a new thread is filed under (it works in that folder); ignored when continuing.
    project_id: str | None = None
    # How far the model may go, from this turn on. Persisted on the thread so switching is
    # a plain send and a reload keeps it. Absent means the thread's current level (new
    # threads get the default), so a client unaware of levels keeps working.
    permission_level: PermissionLevel | None = None
    # The agent this thread runs (`services/agents`); persists and may change on any send.
    # Absent keeps the thread's, new threads get the default.
    agent: str | None = None
    # Set when the operator resumes a bound-stopped turn ("Continue" under a stop marker):
    # the branch node id carrying the marker. Accepting retires the marker durably. Ignored
    # on a new conversation.
    continues_message_id: str | None = None
    # The slash command picked in the composer; the prompt stays the literal
    # `/review the auth route`. Not a pre-expanded prompt: the backend decides what a
    # command means, and a client-sent expansion would be a turn the operator never wrote.
    command: CommandInvocation | None = None


class RestoreFiles(BaseModel):
    """Confirms the file rewind a dry run previewed (``GET /conversations/{id}/changes/rewind``)."""

    preview_token: str


class RegenerateCreate(BaseModel):
    conversation_id: str
    message_id: str  # the assistant turn to re-answer (its branch node id)
    # Optional per-turn model override — regenerate with a different provider/model.
    endpoint_id: str | None = None
    model: str | None = None
    # Put the files back to before the re-answered turn first.
    restore_files: RestoreFiles | None = None


class EditCreate(BaseModel):
    conversation_id: str
    message_id: str  # the user turn to replace (its request node id)
    prompt: str  # the edited message
    endpoint_id: str | None = None
    model: str | None = None
    # An edit is a fresh user request, so it injects attachments like a new message
    # (regenerate adds no request and takes none).
    attachment_ids: list[str] = []
    # Put the files back to before the replaced turn first.
    restore_files: RestoreFiles | None = None


class ChatCreated(BaseModel):
    run_id: str
    conversation_id: str


class ChatQueued(BaseModel):
    """A send into a busy conversation: it went into the inbox instead of starting a run."""

    conversation_id: str
    queued_message_id: str
    # The run it queued behind; None when held by something about to start one (a
    # regenerate's claim).
    run_id: str | None = None


class ChatSettings(BaseModel):
    """Operator-tunable chat preferences. The ``auto_compact*`` fields tune conversation
    compaction, which folds older turns into a summary the thread's own model writes once
    the window fills. Every field is optional on a PUT (omitted ⇒ unchanged) and populated
    on a GET; bounds reject nonsensical values. snake_case out, like the rest of ``/chat``.

    On a PUT, ``null`` is a value (not an omission) for ``wall_clock_timeout_s`` and
    ``subagent_max_concurrent``, so the handler reads ``model_fields_set`` for them. On a
    GET, ``null`` is also meaningful for an ``inactivity_timeout_s`` a deploy disabled."""

    # Forbid extras so a typo'd key is a 422, not a 200 that changed nothing.
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    # Whether to fold older turns, and how full the window must get first. A fraction, the
    # same 0–1 quantity the context meter reports, so the wire has one convention. Above 0
    # (0 would fire on an empty thread) and at most 1 (above it, compaction never fires).
    auto_compact_enabled: bool | None = None
    auto_compact_threshold: float | None = Field(default=None, gt=0, le=1)
    # Minutes a thread must be quiet before the sweep writes its work summary. ``ge=1``:
    # zero would qualify threads the instant a turn finished, summarizing live
    # conversations. The ceiling keeps the window inside a day, past which the band would
    # never appear in a normal week, indistinguishable from the feature being off.
    work_summary_idle_minutes: int | None = Field(
        default=None, ge=1, le=WORK_SUMMARY_IDLE_MINUTES_MAX
    )
    # Fullness at which the composer's ring turns amber, then red; fractions like the
    # compaction threshold. The bounds reject only individually nonsensical values; the
    # `warn` < `alert` ordering is checked against the merged values in the PUT, since
    # either may be omitted.
    context_warn_threshold: float | None = Field(default=None, gt=0, lt=1)
    context_alert_threshold: float | None = Field(default=None, gt=0, le=1)
    # Seconds a turn waits for the first token (the silent stretch while a local model
    # reads a long prompt). A deploy-level ``None`` leaves it to the inactivity watchdog.
    first_token_timeout_s: float | None = Field(default=None, gt=0)
    # Seconds a run may emit nothing before it is stopped. ``gt=0`` (0 would stop every
    # turn); the config default applies when unset, and a deploy-level ``None`` disables it.
    inactivity_timeout_s: float | None = Field(default=None, gt=0)
    # Total seconds a run may take, off unless set. Catches a run that keeps emitting and
    # so refreshes the inactivity watchdog forever. ``null`` removes the bound.
    wall_clock_timeout_s: float | None = Field(default=None, gt=0)
    # Sub-agents running at once; ``null`` (the default) is no cap. ``ge=1``: 0 would
    # disable the feature through a number, which the tool catalog already does honestly.
    subagent_max_concurrent: int | None = Field(default=None, ge=1)
