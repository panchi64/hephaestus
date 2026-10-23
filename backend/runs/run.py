"""The Run: one identified, background-executing unit of work.

Chat turns and agent tasks are all Runs; they differ only in the orchestrator that
drives them. Status follows ``queued → running → {done | blocked | error | cancelled}``,
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
from .events.lifecycle import EndCode
from .inbox import ConversationInbox, QueuedMessage
from .overhead import TurnOverhead
from .prefix import PrefixVerdict
from .stream import RunStream
from .timings import TimingTotals, TurnTimer

# Ceiling on how often ``Run.keepalive`` touches the activity clock; a shorter inactivity
# bound beats proportionally faster (see ``Run._beat``).
_KEEPALIVE_INTERVAL_S = 20.0


class RunStatus(StrEnum):
    queued = "queued"
    running = "running"
    awaiting_input = "awaiting_input"
    done = "done"
    blocked = "blocked"
    error = "error"
    cancelled = "cancelled"


class StopCause(StrEnum):
    operator = "operator"
    parent = "parent"
    time_limit = "time_limit"
    turn_limit = "turn_limit"


TERMINAL_STATUSES = {
    RunStatus.done,
    RunStatus.blocked,
    RunStatus.error,
    RunStatus.cancelled,
}


@dataclass(slots=True)
class PreparingCall:
    """A tool call whose arguments are still streaming in."""

    tool_call_id: str
    name: str
    chars: int = 0
    emitted_chars: int = 0
    announced: bool = False


@dataclass
class Run:
    id: str
    kind: str
    owner_id: str
    stream: RunStream
    # Lets a client holding only a conversation id find the in-flight run
    # (``RunRegistry.active_run_for``).
    conversation_id: str | None = None
    # The model endpoint this turn leases while it executes (`runs/endpoint_gate.py`).
    endpoint: Endpoint | None = None
    # Stored id of the prompt this run answers (a regenerate's re-answered request), so a
    # client opening the thread mid-run can place the streaming answer. None until the
    # orchestrator sets it, and for runs that answer no prompt (a hand-started fold).
    prompt_message_id: str | None = None
    status: RunStatus = RunStatus.queued
    detail: str | None = None
    # Who or what stopped a cancelled or blocked run; None for every other ending.
    stop_cause: StopCause | None = None
    # Which bound blocked the run; becomes `RunEnded.code`.
    end_code: EndCode | None = None
    error: str | None = None
    created_at: datetime = field(default_factory=now_utc)
    started_at: datetime | None = None
    ended_at: datetime | None = None
    last_activity_mono: float = 0.0
    # The inactivity bound this run is supervised under (None ⇒ unbounded), stamped by the
    # registry; ``keepalive`` reads it so the heartbeat can't drift slower than the bound.
    inactivity_timeout_s: float | None = None
    task: asyncio.Task[None] | None = None
    # Work that may finish after the run ends (an auto-title). Held so the loop's weak
    # reference is not the only one, and so the registry can wait on or stop it.
    afterwork: set[asyncio.Task[None]] = field(default_factory=set)
    cancel_requested: bool = False
    metrics: RunMetrics | None = None
    # The active model's context window; None leaves the derived context fields null.
    context_window: int | None = None
    # Severity boundaries for that window, from the same settings read as the rest of the
    # turn. Inert without a window, and the window is inert without them.
    context_thresholds: ContextThresholds = DEFAULT_CONTEXT_THRESHOLDS
    # Where this thread's fold fires and whether it is armed, from the policy the
    # compaction trigger itself uses. None when the turn ran with no compaction policy.
    context_fold: FoldPoint | None = None
    # Lives on the Run, not in `drive_turn`, so it survives park/resume: an approval splits
    # a turn into segments, and a per-segment timer would report only the last.
    timer: TurnTimer = field(default_factory=TurnTimer)
    # Timing totals before this run, read from persisted message rows: time isn't in the
    # replayed history, and this makes the emitted frame cumulative over the thread.
    prior_timings: TimingTotals = field(default_factory=TimingTotals)
    # Standing brief and tool schemas measured on the latest model request. Neither reaches
    # the message history, so they are knowable only while a request is assembled. None
    # before the first request or when measurement failed (the readout is absent, not guessed).
    context_overhead: TurnOverhead | None = None
    # How much of the previous request's cacheable prefix the latest request could reuse,
    # and what moved if not (`runs/prefix.py`). Read off the assembled request, like the
    # overhead. None until the first request.
    prefix_verdict: PrefixVerdict | None = None
    # Messages at the head of the replay that predate the latest compaction (0 if none).
    # A response before the boundary reported its prompt size against a vanished history and
    # would pin the gauge at the pre-fold figure. Set on both folds, read by the metrics builder.
    fold_boundary: int = 0
    # Answer and reasoning characters streamed for the model request in flight, which a
    # retry of that request has to take back off the operator's screen.
    step_text_chars: int = 0
    step_thinking_chars: int = 0
    # Tool calls whose arguments are streaming for the request in flight, by part index.
    preparing_calls: dict[int, PreparingCall] = field(default_factory=dict)
    # Opaque continuation payload for a parked run; the substrate never interprets it.
    parked_payload: object | None = None
    # The conversation's inbox, drained at each model-request boundary. It outlives the
    # run; a run with no conversation gets its own.
    inbox: ConversationInbox | None = None
    # Flushes the orchestrator's partial state before the registry force-cancels the task
    # for a wall-clock/inactivity bound. Called synchronously with the bound's message,
    # on the event-loop turn of the still-running task. A raising hook is swallowed.
    on_timeout: Callable[[str], None] | None = None
    # Persists the partial turn before an operator-requested cancel. Called from the
    # registry's coroutine before ``task.cancel()``; the task is suspended then, so
    # reading its state is race-free. Finalize-only: it must not set ``run.status``, which
    # the registry sets once the cancellation lands. A raising hook is swallowed.
    on_cancel: Callable[[], None] | None = None
    # Cancel counterpart for a parked run (``RunRegistry.cancel``'s parked branch), which
    # has no task to interrupt: persists the parked turn, including the operator's prompt.
    # Called after ``run.status`` is already ``cancelled``. A raising hook is swallowed.
    on_park_cancel: Callable[[], None] | None = None

    def touch(self) -> None:
        """Mark activity now — feeds the inactivity watchdog."""
        self.last_activity_mono = asyncio.get_running_loop().time()

    @asynccontextmanager
    async def keepalive(self) -> AsyncGenerator[None]:
        """Hold the inactivity clock open across one long call that streams nothing.

        Inside this scope the inactivity bound no longer applies, so a call that truly
        wedges is bounded only by the run's wall clock. Wrap the one awaited call, never
        a whole orchestration.
        """
        task = asyncio.create_task(self._beat())
        try:
            yield
        finally:
            _ = task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            self.touch()

    async def _beat(self) -> None:
        interval = _KEEPALIVE_INTERVAL_S
        if self.inactivity_timeout_s:
            # A third of the bound, so a shortened timeout can't outpace the default-sized
            # interval; the floor only stops a nonsensical bound from spinning.
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
        """Await the executing task, which ends on a terminal status or on ``awaiting_input``.

        Parking returns the orchestrator; a resumed run continues on a fresh task. Check
        ``is_terminal`` afterwards for "finished for good".
        """
        if self.task is not None:
            await asyncio.shield(self.task)

    def outlive(self, task: asyncio.Task[None]) -> None:
        """Let ``task`` run on past this run's end (see ``afterwork``)."""
        self.afterwork.add(task)
        task.add_done_callback(self.afterwork.discard)

    async def settle_afterwork(self, timeout_s: float | None = None) -> None:
        """Wait, at most ``timeout_s``, for the work this run left running."""
        if self.afterwork:
            _ = await asyncio.wait(list(self.afterwork), timeout=timeout_s)


# An orchestrator drives one Run: it emits events and may call ``run.block()``
# or raise. Normal return ⇒ done; raise ⇒ error; cancellation ⇒ cancelled.
Orchestrator = Callable[[Run], Awaitable[None]]
