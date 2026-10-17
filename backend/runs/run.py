"""Pillar I — the Run: one identified, background-executing unit of work.

Chat turns and agent tasks are all Runs; they differ only in
the orchestrator that drives them. A Run owns its id/owner/status, its event
stream (buffer + broker), and — once running — the asyncio task executing it.
Status follows ``queued → running → {done | blocked | error | cancelled}``,
with a parked ``awaiting_input`` when a sensitive action needs approval.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from .endpoint_gate import Endpoint
from .events import (
    DEFAULT_CONTEXT_THRESHOLDS,
    ContextThresholds,
    Event,
    EventBody,
    FoldPoint,
    MessageSource,
    RunMetrics,
    now_utc,
)
from .inbox import ConversationInbox, QueuedMessage
from .overhead import TurnOverhead
from .prefix import PrefixVerdict
from .stream import RunStream
from .timings import TimingTotals, TurnTimer

# How often :meth:`Run.keepalive` touches the activity clock while a long, silent call is
# in flight. A ceiling, not a fixed rate: a run held to a shorter inactivity bound beats
# proportionally faster (see ``Run._beat``).
_KEEPALIVE_INTERVAL_S = 20.0


class RunStatus(StrEnum):
    queued = "queued"
    running = "running"
    awaiting_input = "awaiting_input"
    done = "done"
    blocked = "blocked"
    error = "error"
    cancelled = "cancelled"


TERMINAL_STATUSES = {
    RunStatus.done,
    RunStatus.blocked,
    RunStatus.error,
    RunStatus.cancelled,
}


@dataclass
class Run:
    id: str
    kind: str
    owner_id: str
    stream: RunStream
    # The conversation this run drives, when it is a chat turn. Lets a client that
    # has only a conversation id (e.g. after a page reload) find the in-flight run
    # to reattach to — see ``RunRegistry.active_run_for``.
    conversation_id: str | None = None
    # The model endpoint this run's turn leases while it executes (`runs/endpoint_gate.py`).
    endpoint: Endpoint | None = None
    # The stored id of the operator's prompt this run is answering — written down by the
    # orchestrator as the turn begins (or, for a regenerate, the request it re-answers).
    # What lets a client that opens the thread mid-run find the row its streaming answer
    # belongs after, instead of inventing one. None until the orchestrator has it, and for
    # a run that answers no prompt (a hand-started fold).
    prompt_message_id: str | None = None
    status: RunStatus = RunStatus.queued
    detail: str | None = None
    error: str | None = None
    created_at: datetime = field(default_factory=now_utc)
    started_at: datetime | None = None
    ended_at: datetime | None = None
    last_activity_mono: float = 0.0
    # The inactivity bound this run is actually being supervised under (None ⇒ unbounded),
    # stamped by the registry when it starts supervising. Read by ``keepalive`` so a
    # heartbeat can't drift slower than the bound it exists to stay inside.
    inactivity_timeout_s: float | None = None
    task: asyncio.Task[None] | None = None
    cancel_requested: bool = False
    metrics: RunMetrics | None = None
    # The active model's context window, when known — set by the orchestrator so
    # emitted metrics can report how full the window is. None leaves the derived
    # context fields null (no ceiling to measure against).
    context_window: int | None = None
    # The operator's severity boundaries for that window, set by the orchestrator from
    # the same settings read that resolved everything else about the turn. Sits beside
    # `context_window` because they are one measurement: a ceiling nobody set boundaries
    # against, and boundaries with no ceiling to apply them to, are equally inert.
    context_thresholds: ContextThresholds = DEFAULT_CONTEXT_THRESHOLDS
    # Where this thread's fold will fire, and whether it is armed — set by the orchestrator
    # from the same resolved policy the compaction trigger itself uses, so the mark the
    # operator sees on the gauge is the number that will actually act on their thread.
    # None when the turn ran with no compaction policy at all.
    context_fold: FoldPoint | None = None
    # This run's wall-clock stopwatch, collecting a timing per model response as the
    # translator walks the graph. Lives on the Run rather than in `drive_turn` so it
    # survives a park/resume: an approval splits a turn into several segments, and a
    # timer scoped to one of them would report only the last.
    timer: TurnTimer = field(default_factory=TurnTimer)
    # The conversation's timing totals *before* this run — read from the persisted
    # message rows when the turn starts. What makes the emitted frame cumulative over
    # the thread rather than over this run: everything else in the frame is derived
    # from the replayed history, but time isn't in the history, only in our own rows.
    prior_timings: TimingTotals = field(default_factory=TimingTotals)
    # The standing brief and tool schemas measured on this turn's most recent model
    # request — the two parts of a request that never reach the message history, and so
    # are knowable only while one is being assembled. None until the turn makes its first
    # request, and on any turn whose measurement failed; the composition readout is absent
    # rather than guessed in that case.
    context_overhead: TurnOverhead | None = None
    # How much of the previous request's cacheable prefix this turn's most recent request
    # could have reused, and what moved if it couldn't (`runs/prefix.py`). Sits beside the
    # overhead because it is read off the same assembled request by the same kind of
    # capability, and for the same reason: neither the brief nor the tool array reaches the
    # message history, so whether *this* brief is the same brief as last time is knowable
    # only while a request is being assembled. None until the turn makes its first request.
    prefix_verdict: PrefixVerdict | None = None
    # How many messages at the head of the current replay predate this run's most recent
    # compaction — 0 when nothing has folded. A response *before* the boundary reported its
    # prompt size against a history that no longer exists, so reading the footprint off it
    # would pin the gauge at the pre-fold figure for the rest of the turn. Set by the
    # orchestrator on both folds (the prelude's, and the in-turn overflow recovery's), read
    # only by the metrics builder.
    fold_boundary: int = 0
    # Set once the first answer token has streamed. The AE-5.3 rule — never
    # switch endpoints after answer text has begun — is enforced against this:
    # the orchestrator refuses to re-drive a turn onto another endpoint once it
    # is set (FallbackModel only falls back pre-stream; this guards the rest).
    answer_started: bool = False
    # Answer and reasoning characters streamed for the model request in flight, which a
    # retry of that request has to take back off the operator's screen.
    step_text_chars: int = 0
    step_thinking_chars: int = 0
    # Opaque continuation payload for a parked run (set by the orchestrator
    # layer when awaiting approval). The substrate never interprets it.
    parked_payload: object | None = None
    # The conversation's inbox, which the orchestrator drains at each model-request
    # boundary. The registry hands a conversation's run the conversation's own inbox, which
    # outlives the run; a run with no conversation gets one of its own.
    inbox: ConversationInbox | None = None
    # Opaque hook the orchestrator may set to flush whatever partial state it holds
    # before the registry force-cancels this run's task for a wall-clock/inactivity
    # bound — called synchronously with the bound's operator-legible message, from
    # inside the still-running task's own event-loop turn (safe to read the task's
    # local state). The substrate never interprets it beyond calling it; a raising
    # hook is swallowed by the caller.
    on_timeout: Callable[[str], None] | None = None
    # The cancel counterpart of ``on_timeout`` — set by the orchestrator to persist
    # whatever partial turn it holds before the registry cancels this run's task for
    # an operator-requested cancel. Called synchronously from the *registry's own*
    # coroutine (not this run's task), before ``task.cancel()``: under single-threaded
    # asyncio, this run's task is necessarily suspended while a different coroutine is
    # running, so reading its state here is race-free. Unlike ``on_timeout`` it must
    # never set ``run.status``/outcome — the registry's own cancellation handling sets
    # the terminal ``cancelled`` status once the cancellation actually lands, so this
    # hook is finalize-only. A raising hook is swallowed by the caller.
    on_cancel: Callable[[], None] | None = None
    # The parked counterpart of ``on_cancel``: set by the orchestrator once a turn has
    # parked (``awaiting_input``) awaiting an approval decision, so cancelling the
    # *parked* run — there is no task left to interrupt, see ``RunRegistry.cancel``'s
    # parked branch — still persists the parked turn instead of silently dropping it
    # (including the operator's own prompt). Called synchronously from the registry's
    # own coroutine, after ``run.status`` has already been set to the terminal
    # ``cancelled`` value. A raising hook is swallowed by the caller.
    on_park_cancel: Callable[[], None] | None = None

    def touch(self) -> None:
        """Mark activity now — feeds the inactivity watchdog."""
        self.last_activity_mono = asyncio.get_running_loop().time()

    @asynccontextmanager
    async def keepalive(self) -> AsyncGenerator[None]:
        """Hold the inactivity clock open across a single long call that streams nothing.

        The watchdog reads activity from the event stream, which is the right signal
        almost everywhere: a run that has emitted nothing for minutes has stalled. The
        exception is one awaited call that is known to be slow and known to produce no
        frames until it finishes — a non-streaming model call generating a long report,
        say. Between two step boundaries there is no activity to observe even while real
        work is happening, and the run would be stopped for idling.

        This belongs to the substrate, not to whichever feature hit it first: the clock is
        the substrate's, and a heartbeat loop reimplemented per long call is a heartbeat
        loop that drifts out of step with the operator's configured bound.

        The bargain is explicit: inside this scope the inactivity bound no longer applies,
        so a call that truly wedges is bounded only by the run's wall clock. Wrap the one
        awaited call, never a whole orchestration.
        """
        task = asyncio.create_task(self._beat())
        try:
            yield
        finally:
            _ = task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            self.touch()  # the call returned — that is real activity

    async def _beat(self) -> None:
        interval = _KEEPALIVE_INTERVAL_S
        if self.inactivity_timeout_s:
            # Comfortably inside whatever bound this run is actually being held to, so a
            # shortened timeout can't outpace a fixed interval chosen against the default.
            # The small floor is only to keep a nonsensical bound from spinning; at any
            # realistic timeout the ceiling above is what applies.
            interval = min(interval, max(self.inactivity_timeout_s / 3, 0.01))
        while True:
            await asyncio.sleep(interval)
            self.touch()

    def emit(self, body: EventBody) -> Event:
        self.touch()
        return self.stream.emit(body)

    def block(self, detail: str | None = None) -> None:
        """Orchestrator declares it cannot proceed."""
        self.status = RunStatus.blocked
        self.detail = detail

    def park(self, payload: object | None = None) -> None:
        """Park awaiting operator input (approval); not a terminal state.

        The orchestrator returns after parking; the registry leaves the stream
        open and the slot free until the run is resumed or cancelled.
        """
        self.status = RunStatus.awaiting_input
        self.parked_payload = payload

    @property
    def messages(self) -> ConversationInbox:
        """This run's inbox; a run built outside the registry gets one of its own."""
        if self.inbox is None:
            self.inbox = ConversationInbox(self.owner_id, sink=self._emit_open)
        return self.inbox

    @property
    def pending_messages(self) -> list[QueuedMessage]:
        return list(self.messages.messages)

    def enqueue_message(
        self, text: str, *, source: MessageSource = "operator", sender: str | None = None
    ) -> QueuedMessage:
        return self.messages.enqueue(text, source=source, sender=sender)

    def drain_messages(self) -> list[QueuedMessage]:
        return self.messages.drain()

    def _emit_open(self, body: EventBody) -> None:
        if not self.stream.closed:
            _ = self.emit(body)

    def set_metrics(self, metrics: RunMetrics) -> None:
        """Stash final metrics; the registry emits them at terminal."""
        self.metrics = metrics

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def is_parked(self) -> bool:
        return self.status is RunStatus.awaiting_input

    async def wait(self) -> None:
        """Await the executing task — it ends when the Run settles, which means either a
        terminal status **or** ``awaiting_input``: parking returns the orchestrator, so
        the task finishes there too and the run continues on a fresh task if it resumes.
        Callers that need "finished for good" must check ``is_terminal`` afterwards."""
        if self.task is not None:
            await asyncio.shield(self.task)


# An orchestrator drives one Run: it emits events and may call ``run.block()``
# or raise. Normal return ⇒ done; raise ⇒ error; cancellation ⇒ cancelled.
Orchestrator = Callable[[Run], Awaitable[None]]
