"""Persisting a turn that was stopped from outside.

Three things can end a turn without its own code path reaching ``finalize``:

- a **wall-clock or inactivity bound** — the registry calls ``run.on_timeout`` and then
  force-cancels the task;
- an **operator cancel** — the registry calls ``run.on_cancel`` from its own coroutine,
  before cancelling;
- an **unhandled exception** unwinding the orchestrator.

All three must persist the same thing, and at minimum the operator's own message: a turn
that vanishes on reload — question included — is the worst outcome any of these can have.
The prompt is written down when the turn begins (``finalize`` records only what follows
it), so a stop before the first token leaves the prompt on record with its stop marker
rather than depending on this module to rescue it.
Both orchestrators (a fresh chat turn, and the resume of a parked one) had their own copy
of all three, ~75 lines that had to stay in step by hand.

They differ in exactly two ways, which is what this takes as arguments: where the partial
messages come from, and how the persistence context is known. A resume's context is fixed
up front (it rides on the ``ParkedTurn``); a chat turn's isn't — ``start`` isn't known
until the history has been loaded and compacted — so it is supplied as a callable and read
at flush time rather than captured at arm time.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from pydantic_ai import ModelMessage

from runs import Run

from .history import TurnStart

# Persistent stop markers for the cancel/unhandled-error flush paths (mirrors the
# bound-hit details the registry builds — a plain sentence, not internal jargon — stamped
# via `blocked_reason` so a reload shows the same explanation the live stream did, without
# touching `run.status`, which the registry itself decides).
CANCELLED_DETAIL = "cancelled by the operator"
ERRORED_DETAIL = "an unexpected error stopped this turn"


@dataclass(frozen=True)
class PersistContext:
    """Everything ``finalize`` needs about *where* a turn goes, as opposed to what it
    contains. Rebuilt per flush by the chat orchestrator (whose ``start`` is only known
    once the turn is under way) and constant for a resume.

    ``start`` is a :class:`~agent.history.TurnStart` rather than a bare index because a
    mid-turn fold can leave the turn beginning *inside* a message whose first parts belong
    to the history in front of it. It also names the operator's prompt when the turn
    already wrote it down (``start.prompt_message_id``), which is why nothing about the
    prompt's attachments, references or command appears here: they were stamped on that
    write, and what is recorded now is only what followed it."""

    conversation_id: str | None
    start: TurnStart
    clean_drop: tuple[int, int] | None = None


@dataclass
class TurnFlush:
    """Arms ``run.on_timeout``/``run.on_cancel`` and performs the error flush.

    ``messages`` returns whatever the turn holds right now — it must never return an empty
    list where the operator's own prompt exists, or a stop before the first model step
    persists nothing and the turn disappears. ``context`` is read at flush time, not at
    arm time, so a chat turn can arm its hooks in the prelude (before a bound could trip)
    and still flush against the boundary it later computed.

    ``record`` is injected rather than imported: it is a closure the orchestrator supplies
    that already knows its store, so this module stays out of the engine's import graph
    and never has to construct the engine's own turn type.
    """

    run: Run
    messages: Callable[[], list[ModelMessage]]
    context: Callable[[], PersistContext]
    record: Callable[[list[ModelMessage], str, PersistContext], None]
    # True once the turn has been recorded by any path, so the error flush and the hooks
    # can't double-record it (or stamp a spurious stop on a completed answer).
    done: bool = False

    def arm(self) -> None:
        self.run.on_timeout = self._on_timeout
        self.run.on_cancel = self._on_cancel

    def disarm(self) -> None:
        """Called once the turn is recorded. The hooks fire from outside this task, so
        leaving them armed through the post-answer window (titling, say) would let a late
        bound re-record a turn that already completed."""
        self.run.on_timeout = None
        self.run.on_cancel = None

    def _on_timeout(self, detail: str) -> None:
        # `detail` is already the operator-legible message the registry built
        # (`RunTimeout.__str__`, from the bound's configured duration) — reused verbatim
        # so the persisted marker matches the toast the live stream showed.
        self._write(detail, block=True)

    def _on_cancel(self) -> None:
        # The cancel counterpart of `_on_timeout`. It must *not* call `run.block(...)`:
        # the registry's own `except asyncio.CancelledError` sets the terminal `cancelled`
        # status once the cancellation lands, and blocking here would clobber it.
        self._write(CANCELLED_DETAIL, block=False)

    def flush_error(self) -> None:
        """The unwinding-exception path. Never touches ``run.status`` — for an unhandled
        exception the registry decides the terminal outcome."""
        if self.done:
            return
        self._write(ERRORED_DETAIL, block=False)

    def _write(self, detail: str, *, block: bool) -> None:
        messages = self.messages()
        if not messages:
            return  # nothing to persist yet — a stop this early has no turn to record
        if block:
            self.run.block(detail)
        self.record(messages, detail, self.context())
        self.done = True
