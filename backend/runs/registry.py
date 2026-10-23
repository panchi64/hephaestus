"""The RunRegistry: launch, track, bound, and cancel Runs.

Runs are in-process asyncio tasks gated by a per-lane concurrency semaphore (bursts wait
in ``queued``); the lane follows from the run's ``kind`` (``runs/lanes.py``). The registry
owns queued→running, the terminal-state mapping, the wall-clock and inactivity bounds, and
cancellation, so every orchestrator inherits them.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from contextlib import suppress
from enum import Enum
from typing import Literal
from uuid import uuid4

from core.tasks import STOP_TIMEOUT_S

from .endpoint_gate import Endpoint, EndpointGate
from .events import EventBody, LimitNotice, RunEnded, RunError, RunQueued, RunStarted, now_utc
from .inbox import NO_JOURNAL, ConversationInbox, InboxDispatch, InboxJournal
from .lanes import LaneGate, LaneLimits
from .parked import NO_PARKED_JOURNAL, ParkedJournal
from .run import Orchestrator, Run, RunStatus, StopCause
from .stream import RunStream

logger = logging.getLogger(__name__)

#: How many inboxes are kept before idle ones are dropped.
_INBOXES_KEPT = 256


class Unset(Enum):
    UNSET = object()


UNSET = Unset.UNSET


def _ended(run: Run) -> RunEnded:
    match run.status:
        case RunStatus.done:
            return RunEnded(outcome="done", detail=run.detail)
        case RunStatus.blocked:
            return RunEnded(outcome="blocked", detail=run.detail, code=run.end_code)
        case RunStatus.cancelled:
            return RunEnded(outcome="cancelled", detail=run.detail)
        case _:
            raise ValueError(f"run {run.id} has no ended outcome for status {run.status}")


class ConversationBusyError(Exception):
    """Raised by ``claim`` or ``submit`` when a live run or another in-flight claim already
    holds ``conversation_id``; the atomic backstop behind the routes' 409 guards
    (``deps.claim_conversation``).

    ``submit`` is atomic on its own (no ``await`` between check and register), but a route
    with a real ``await`` before its own mutation or submit (a model resolve, an orphan
    lookup) can have two requests both see "no live run". ``claim`` closes that: take it
    before the first such ``await`` and release it in a ``finally``.
    """

    def __init__(self, conversation_id: str) -> None:
        super().__init__(f"a run is already in progress for conversation {conversation_id!r}")
        self.conversation_id: str = conversation_id


type TimeoutKind = Literal["wall_clock", "inactivity"]


class RunTimeout(Exception):
    """A Run exceeded a bound. ``kind`` is ``wall_clock`` or ``inactivity``.

    The message reaches the operator verbatim (toast and ``blockedDetail`` marker), so it is
    built from the configured duration as a plain sentence.
    """

    def __init__(self, kind: TimeoutKind, bound_s: float | None = None) -> None:
        super().__init__(_timeout_message(kind, bound_s))
        self.kind: TimeoutKind = kind


def _fmt_minutes(minutes: float) -> str:
    """Minutes as a whole number when exact, else one decimal. Not ``round()``, whose
    half-to-even (``round(2.5) == 2``) would misstate a bound like 150s."""
    if minutes.is_integer():
        return str(int(minutes))
    return f"{minutes:.1f}"


def adj_duration(bound_s: float) -> str:
    """A hyphenated adjective phrase for a duration, e.g. ``30-minute``/``45-second``."""
    minutes = bound_s / 60
    if minutes >= 1:
        return f"{_fmt_minutes(minutes)}-minute"
    return f"{max(1, round(bound_s))}-second"


def count_duration(bound_s: float) -> str:
    """A counted-noun phrase for a duration, e.g. ``2 minutes``/``1 second``."""
    minutes = bound_s / 60
    if minutes >= 1:
        label = _fmt_minutes(minutes)
        return f"{label} {'minute' if label == '1' else 'minutes'}"
    n = max(1, round(bound_s))
    return f"{n} {'second' if n == 1 else 'seconds'}"


def _timeout_message(kind: str, bound_s: float | None) -> str:
    if kind == "wall_clock":
        if not bound_s:
            return "this run hit its overall time limit"
        return f"this run hit the {adj_duration(bound_s)} overall limit"
    if kind == "inactivity":
        if not bound_s:
            return "no activity for too long"
        return f"no activity for {count_duration(bound_s)}"
    return f"{kind} timeout exceeded"


def _queued(run: Run, position: int) -> None:
    if run.endpoint is not None and not run.stream.closed:
        _ = run.emit(RunQueued(position=position, endpoint=run.endpoint.name))


class RunRegistry:
    def __init__(
        self,
        *,
        lanes: LaneLimits | None = None,
        endpoints: EndpointGate | None = None,
        wall_clock_timeout_s: float | None = None,
        inactivity_timeout_s: float | None = None,
        max_retained: int = 200,
        on_terminal: Callable[[Run], None] | None = None,
    ) -> None:
        self._runs: dict[str, Run] = {}
        self._lanes: LaneGate = LaneGate(lanes)
        self._endpoints: EndpointGate = endpoints or EndpointGate()
        self._wall_clock: float | None = wall_clock_timeout_s
        self._inactivity: float | None = inactivity_timeout_s
        self._max_retained: int = max_retained
        # (owner_id, conversation_id) pairs held via `claim`: in-flight requests that may
        # never register a Run but must still block a second mutation on the conversation.
        self._claims: set[tuple[str, str]] = set()
        # Fired once per run at its terminal transition, before the stream closes, so
        # `runs/` needn't import `services/` (app.py composes the emit policy). Synchronous
        # and best-effort: see `_fire_terminal`.
        self._on_terminal: Callable[[Run], None] | None = on_terminal
        # One inbox per (owner, conversation), outliving every run on it.
        self._inboxes: dict[tuple[str, str], ConversationInbox] = {}
        self._journal: InboxJournal = NO_JOURNAL
        self._parked_journal: ParkedJournal = NO_PARKED_JOURNAL
        # Runs whose park is on the journal, so ending one that never parked costs no write.
        self._journalled: set[str] = set()
        # Opaque to the substrate: the orchestrator layer files and reads these by run kind.
        self._recomposers: dict[str, object] = {}
        self._restore_checks: dict[str, Callable[[Run], Awaitable[str | None]]] = {}
        self._closing: bool = False
        self._parks_restored: asyncio.Event = asyncio.Event()

    @property
    def parks_restored(self) -> asyncio.Event:
        """Set once the stored parks are back (or the attempt ended), so a holder of a parked
        run's record can tell a live run from an orphan."""
        return self._parks_restored

    @property
    def closing(self) -> bool:
        """Set once shutdown begins: no turn starts from a queued message, so inboxes and
        journal stay as they were for the next boot."""
        return self._closing

    @property
    def endpoints(self) -> EndpointGate:
        return self._endpoints

    # --- inboxes --------------------------------------------------------------
    def attach_journal(self, journal: InboxJournal) -> None:
        """Write every inbox through ``journal`` from now on."""
        self._journal = journal
        for inbox in self._inboxes.values():
            inbox.journal = journal

    # --- parked runs ----------------------------------------------------------
    def attach_parked_journal(self, journal: ParkedJournal) -> None:
        """Write every run that parks through ``journal`` from now on."""
        self._parked_journal = journal

    def set_recomposer(self, kind: str, hook: object) -> None:
        """File what rebuilds a parked run of ``kind`` after a restart."""
        self._recomposers[kind] = hook

    def recomposer(self, kind: str) -> object | None:
        return self._recomposers.get(kind)

    def restore_parked(self, run: Run, announcements: Sequence[EventBody]) -> None:
        """Track a run rebuilt from a stored park, in ``awaiting_input``, and replay what it
        was waiting on into its stream. The row is already stored, so it is not rewritten.

        The caller has checked that the conversation has no live run, with no await between
        that check and this call; nothing here re-checks."""
        if run.conversation_id is not None:
            run.inbox = self.inbox(run.conversation_id, run.owner_id)
        self.register(run)
        self._journalled.add(run.id)
        for body in announcements:
            _ = run.emit(body)

    def end_unrestorable(self, run: Run, detail: str) -> None:
        """Record a run from a stored park that cannot be resumed as ended ``blocked``, so
        everything that waits on a run's end hears of it. Its row is dropped."""
        self.register(run)
        self._journalled.add(run.id)
        self._block_parked(run, detail)

    def set_restore_check(self, kind: str, check: Callable[[Run], Awaitable[str | None]]) -> None:
        """File what vets a restored park of ``kind``: it returns why the park can no longer
        be resumed, or ``None``."""
        self._restore_checks[kind] = check

    async def restore_problem(self, run: Run) -> str | None:
        check = self._restore_checks.get(run.kind)
        return None if check is None else await check(run)

    async def block_parked(self, run_id: str, detail: str) -> bool:
        """End a parked run ``blocked``: its turn is kept as a stopped one, nothing it
        awaited runs, and the terminal hooks fire as they do for any ending."""
        run = self._runs.get(run_id)
        if run is None or run.status is not RunStatus.awaiting_input:
            return False
        self._block_parked(run, detail)
        return True

    def _block_parked(self, run: Run, detail: str) -> None:
        run.block(detail)
        self._flush_park_cancel(run)
        self._forget_park(run.id)
        _ = run.emit(RunEnded(outcome="blocked", detail=detail))
        run.ended_at = now_utc()
        self._fire_terminal(run)
        run.stream.close()
        if run.task is not None and not run.task.done():
            _ = run.task.cancel()

    def _journal_park(self, run: Run) -> bool:
        try:
            stored = self._parked_journal.record(run)
        except Exception:
            logger.exception("runs: could not store the park of run %s", run.id)
            stored = False
        if stored:
            self._journalled.add(run.id)
        return stored

    def _forget_park(self, run_id: str) -> None:
        if run_id in self._journalled:
            self._journalled.discard(run_id)
            self._parked_journal.forget(run_id)

    def inbox(self, conversation_id: str, owner_id: str) -> ConversationInbox:
        """The inbox of ``conversation_id``. Its events go to the live run on the thread."""
        key = (owner_id, conversation_id)
        found = self._inboxes.get(key)
        if found is None:

            def sink(body: EventBody) -> None:
                run = self.active_run_for(conversation_id, owner_id)
                if run is not None and not run.stream.closed:
                    _ = run.emit(body)

            found = ConversationInbox(owner_id, conversation_id, sink=sink, journal=self._journal)
            self._prune_inboxes()
            self._inboxes[key] = found
        return found

    def _prune_inboxes(self) -> None:
        """Drop idle inboxes once there are many. A live or claimed thread's stays, since a
        second inbox would split its queue."""
        if len(self._inboxes) < _INBOXES_KEPT:
            return
        for key, inbox in list(self._inboxes.items()):
            owner_id, conversation_id = key
            if (
                inbox.idle
                and key not in self._claims
                and self.active_run_for(conversation_id, owner_id) is None
            ):
                del self._inboxes[key]

    def inboxes(self) -> list[ConversationInbox]:
        """Every inbox holding something, or waiting on its operator."""
        return [
            inbox
            for inbox in self._inboxes.values()
            if inbox.messages or inbox.waiting_for_operator
        ]

    # --- lookup ---------------------------------------------------------------
    def get(self, run_id: str) -> Run | None:
        return self._runs.get(run_id)

    def list(self, owner_id: str | None = None) -> list[Run]:
        runs = list(self._runs.values())
        if owner_id is not None:
            runs = [r for r in runs if r.owner_id == owner_id]
        return runs

    def active_run_for(self, conversation_id: str, owner_id: str) -> Run | None:
        """The most recent non-terminal run driving ``conversation_id``, if any.

        How a reloading client finds the run to replay: a streaming answer isn't persisted
        until it finishes (its prompt is).
        """
        candidates = [
            r
            for r in self._runs.values()
            if r.owner_id == owner_id and r.conversation_id == conversation_id and not r.is_terminal
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda r: r.created_at)

    def busy(self, owner_id: str) -> bool:
        """Whether any of the owner's runs is live or about to start."""
        return any(key[0] == owner_id for key in self._claims) or any(
            r.owner_id == owner_id and not r.is_terminal for r in self._runs.values()
        )

    # --- claim (pre-submit / pre-mutation mutual exclusion) --------------------
    def claim(self, conversation_id: str, owner_id: str) -> None:
        """Check-and-claim ``conversation_id`` for a request about to reposition the active
        leaf (regenerate/edit/rewind/switch-version/purging delete) or submit a run, with
        its own ``await``s still ahead.

        Synchronous, so two near-simultaneous callers can't both pass; the first wins.
        Raises :class:`ConversationBusyError` if a live run drives the conversation or
        another request holds the claim.

        The caller must `release` in a ``finally`` covering every exit path, or the
        conversation stays "busy" forever.
        """
        key = (owner_id, conversation_id)
        if key in self._claims or self.active_run_for(conversation_id, owner_id) is not None:
            raise ConversationBusyError(conversation_id)
        self._claims.add(key)

    def release(self, conversation_id: str, owner_id: str) -> None:
        """Release a claim taken by `claim`; idempotent, even if `claim` raised."""
        self._claims.discard((owner_id, conversation_id))

    def release_and_kick(
        self, conversation_id: str, owner_id: str, dispatcher: InboxDispatch | None
    ) -> None:
        """Release a claim; one that started no run leaves whatever queued behind it to the
        inbox dispatcher."""
        self.release(conversation_id, owner_id)
        if dispatcher is not None and self.active_run_for(conversation_id, owner_id) is None:
            dispatcher.kick(owner_id, conversation_id)

    # --- launch ---------------------------------------------------------------
    def register(self, run: Run) -> None:
        """Track an already-built run without launching a task for it."""
        self._runs[run.id] = run

    def submit(
        self,
        *,
        kind: str,
        owner_id: str,
        orchestrator: Orchestrator,
        run_id: str | None = None,
        conversation_id: str | None = None,
        endpoint: Endpoint | None = None,
        wall_clock_timeout_s: float | None | Unset = UNSET,
        inactivity_timeout_s: float | None | Unset = UNSET,
    ) -> Run:
        # No ``await`` between this check and registering, closing the race a caller's own
        # pre-submit guard can't: two requests that saw no active run can both reach here.
        if conversation_id is not None and (
            self.active_run_for(conversation_id, owner_id) is not None
        ):
            raise ConversationBusyError(conversation_id)
        run = Run(
            id=run_id or uuid4().hex,
            kind=kind,
            owner_id=owner_id,
            conversation_id=conversation_id,
            endpoint=endpoint,
            stream=RunStream(),
        )
        if conversation_id is not None:
            run.inbox = self.inbox(conversation_id, owner_id)
        self.register(run)
        self._evict_old()
        wall = self._wall_clock if wall_clock_timeout_s is UNSET else wall_clock_timeout_s
        idle = self._inactivity if inactivity_timeout_s is UNSET else inactivity_timeout_s
        run.task = asyncio.create_task(
            self._execute(run, orchestrator, wall, idle),
            name=f"run:{run.id}",
        )
        return run

    async def cancel(self, run_id: str) -> bool:
        """Request cancellation.

        Armed twice: ``run.cancel_requested`` is a flag the orchestrator checks at its
        next step boundary (``agent/turn.py``), and ``task.cancel()`` is the immediate
        backstop.

        Idempotent: a running run stays ``running`` until its ``CancelledError`` lands on a
        later loop tick, and ``cancel_requested`` already being set means a cancel is in
        flight, so a repeat must not re-flush or re-persist.
        """
        run = self._runs.get(run_id)
        if run is None or run.is_terminal:
            return False
        if run.status is RunStatus.awaiting_input:
            # Parked: no task to interrupt, but its persistence is otherwise recorded only
            # on resume, so flush first (`_flush_park_cancel`) or the prompt and completed
            # tool calls are dropped.
            run.cancel_requested = True
            run.status = RunStatus.cancelled
            self._flush_park_cancel(run)
            self._forget_park(run.id)
            _ = run.emit(RunEnded(outcome="cancelled"))
            run.ended_at = now_utc()
            self._fire_terminal(run)
            run.stream.close()
            # Between `park()` and the orchestrator's return the task still unwinds (settling
            # the namer); interrupt it or it resumes into an already-finalized run.
            if run.task is not None and not run.task.done():
                _ = run.task.cancel()
            return True
        if run.cancel_requested:
            # Already flushed and cancelled; don't repeat the non-idempotent flush.
            return True
        run.cancel_requested = True
        self._flush_cancel(run)
        if run.task is not None:
            _ = run.task.cancel()
        return True

    async def shutdown(self, timeout_s: float = 5.0) -> None:
        """Cancel every live unparked run and wait up to ``timeout_s`` for them to unwind.

        Registered last in the app lifecycle so it stops first: orchestrators write through
        stores (above all the write-behind drainer), and a submit onto a stopped drainer is
        discarded silently. Cancelling here runs each turn's pre-cancel flush while the
        stores are live.

        A run parked on the operator is left as is (no flush, no ``run.ended``) and its park
        is stored at the stream position it ends on, for the next process. One still
        unwinding into its park is waited for so the park is journalled before the stores
        stop. A park that can't be stored is cancelled like a running turn.
        """
        self._closing = True
        live = [run for run in self._runs.values() if not run.is_terminal and not run.is_parked]
        for run in live:
            _ = await self.cancel(run.id)
        unwinding = [
            run.task
            for run in self._runs.values()
            if run.is_parked and run.task is not None and not run.task.done()
        ]
        pending = [
            *(run.task for run in live if run.task is not None and not run.task.done()),
            *unwinding,
        ]
        if pending:
            _, stuck = await asyncio.wait(pending, timeout=timeout_s)
            if stuck:
                logger.warning("runs: %d run(s) still unwinding after %.1fs", len(stuck), timeout_s)
        # A park is kept only if its row is queued, at the stream position it ends on;
        # any other parked run is cancelled, which persists its turn.
        for run in list(self._runs.values()):
            if run.status is RunStatus.awaiting_input and not self._journal_park(run):
                self._forget_park(run.id)
                _ = await self.cancel(run.id)
        # Afterwork writes through the same stores, so it stops while they are still live.
        afterwork = [task for run in self._runs.values() for task in run.afterwork]
        for task in afterwork:
            _ = task.cancel()
        if afterwork:
            _ = await asyncio.wait(afterwork, timeout=STOP_TIMEOUT_S)

    async def settle_afterwork(
        self, conversation_id: str, owner_id: str, timeout_s: float | None = None
    ) -> None:
        """Wait, at most ``timeout_s``, for work this conversation's runs left running."""
        afterwork = [
            task
            for run in self._runs.values()
            if run.owner_id == owner_id and run.conversation_id == conversation_id
            for task in run.afterwork
        ]
        if afterwork:
            _ = await asyncio.wait(afterwork, timeout=timeout_s)

    async def resume(
        self,
        run_id: str,
        orchestrator: Orchestrator,
        *,
        wall_clock_timeout_s: float | None | Unset = UNSET,
        inactivity_timeout_s: float | None | Unset = UNSET,
    ) -> Run | None:
        """Continue a parked run with a fresh orchestrator on the same Run and stream (no new
        ``run.started``), under fresh bounds."""
        run = self._runs.get(run_id)
        if run is None or run.status is not RunStatus.awaiting_input:
            return None
        # The parked task must exit before status changes, or it could wake and wrongly finalize.
        if run.task is not None:
            with suppress(asyncio.CancelledError):
                await run.task
        if run.status is not RunStatus.awaiting_input:
            return None  # cancelled (or already resumed) while we waited
        run.status = RunStatus.queued
        self._forget_park(run.id)
        wall = self._wall_clock if wall_clock_timeout_s is UNSET else wall_clock_timeout_s
        idle = self._inactivity if inactivity_timeout_s is UNSET else inactivity_timeout_s
        run.task = asyncio.create_task(
            self._execute(run, orchestrator, wall, idle, resuming=True),
            name=f"run:{run.id}:resume",
        )
        return run

    # --- execution ------------------------------------------------------------
    async def _execute(
        self,
        run: Run,
        orchestrator: Orchestrator,
        wall_clock: float | None,
        inactivity: float | None,
        *,
        resuming: bool = False,
    ) -> None:
        try:
            # Waits ``queued`` in the run's own lane, so a saturated lane never holds up
            # another, then for its model endpoint (`runs/endpoint_gate.py`).
            async with (
                self._lanes.slot(run.kind),
                self._endpoints.lease(
                    run.endpoint, run.kind, on_position=lambda position: _queued(run, position)
                ),
            ):
                run.status = RunStatus.running
                run.touch()
                if not resuming:
                    run.started_at = now_utc()
                    _ = run.emit(RunStarted(run_id=run.id, kind=run.kind))
                await self._supervise(run, orchestrator, wall_clock, inactivity)
                if run.is_parked:
                    # Stream stays open and the slot frees; resume or cancel comes out of band.
                    _ = self._journal_park(run)
                    return
                if not run.is_terminal:
                    run.status = RunStatus.done
                self._emit_metrics(run)
                self._emit(run, _ended(run))
        except RunTimeout as timeout:
            # Ends like a usage-limit stop (LimitNotice + blocked RunEnded), not RunError:
            # a timeout is an expected bound, rendered as a toast and a persistent marker.
            self._emit(run, LimitNotice(limit="time", message=str(timeout)))
            parked = run.status is RunStatus.awaiting_input
            run.block(str(timeout))
            run.end_code = timeout.kind
            if timeout.kind == "wall_clock":
                run.stop_cause = StopCause.time_limit
            if parked:
                # Tripped after the park but before the task unwound: persistence was
                # deferred to a resume that won't happen and `on_timeout` is already
                # disarmed, so the whole turn would be dropped.
                self._flush_park_cancel(run)
            self._emit_metrics(run)
            self._emit(run, _ended(run))
        except asyncio.CancelledError:
            # Recorded as a terminal state rather than propagated, on purpose.
            run.status = RunStatus.cancelled
            self._emit_metrics(run)
            self._emit(run, RunEnded(outcome="cancelled"))
        except Exception as exc:  # orchestrator failures are terminal, not fatal
            logger.warning("run %s failed", run.id, exc_info=True)
            run.status = RunStatus.error
            run.error = str(exc)
            self._emit_metrics(run)
            self._emit(run, RunError(message=str(exc), kind=type(exc).__name__))
        finally:
            # A parked run keeps its stream open. A closed stream means a concurrent
            # `cancel` already finalized it (see `_emit`), so don't fire twice.
            if run.is_terminal:
                self._forget_park(run.id)
            if run.is_terminal and not run.stream.closed:
                run.ended_at = run.ended_at or now_utc()
                self._fire_terminal(run)
                run.stream.close()

    @classmethod
    def _emit_metrics(cls, run: Run) -> None:
        """The metrics frame preceding a closing frame, when the run measured anything.

        Every terminal path reports what was spent, including stopped or failed turns. A run
        that measured nothing emits no frame: the client reads a frame as the thread's
        authoritative state, so an empty one would blank the meter and zero the stats."""
        if run.metrics is not None:
            cls._emit(run, run.metrics)

    @staticmethod
    def _emit(run: Run, body: EventBody) -> None:
        """Emit a lifecycle frame unless the stream is closed.

        A parked run is visible while its task unwinds, so `cancel`'s parked branch can close
        the stream under this coroutine; emitting then would raise, flip ``cancelled`` to
        ``error`` and fire the terminal hook twice.
        """
        if not run.stream.closed:
            _ = run.emit(body)

    async def _supervise(
        self,
        run: Run,
        orchestrator: Orchestrator,
        wall_clock: float | None,
        inactivity: float | None,
    ) -> None:
        """Run the orchestrator under wall-clock + inactivity bounds."""
        # So `Run.keepalive` beats inside the bound instead of a fixed interval.
        run.inactivity_timeout_s = inactivity
        if not wall_clock and not inactivity:
            await orchestrator(run)
            return

        loop = asyncio.get_running_loop()
        main = asyncio.ensure_future(orchestrator(run))
        deadline = loop.time() + wall_clock if wall_clock else None
        try:
            while True:
                now = loop.time()
                waits: list[float] = []
                if deadline is not None:
                    waits.append(deadline - now)
                if inactivity is not None:
                    waits.append(run.last_activity_mono + inactivity - now)
                timeout = min(waits) if waits else None
                done, _ = await asyncio.wait(
                    {main}, timeout=max(0.0, timeout) if timeout is not None else None
                )
                if main in done:
                    main.result()  # propagate any orchestrator exception
                    return
                now = loop.time()
                if deadline is not None and now >= deadline:
                    timeout_exc = RunTimeout("wall_clock", wall_clock)
                    self._flush_timeout(run, str(timeout_exc))
                    raise timeout_exc
                if inactivity is not None and now >= run.last_activity_mono + inactivity:
                    timeout_exc = RunTimeout("inactivity", inactivity)
                    self._flush_timeout(run, str(timeout_exc))
                    raise timeout_exc
        finally:
            if not main.done():
                _ = main.cancel()
                with suppress(asyncio.CancelledError):
                    await main

    @staticmethod
    def _flush_timeout(run: Run, message: str) -> None:
        """Let the orchestrator persist its partial state before ``_supervise``'s ``finally``
        force-cancels its task; otherwise a bound reports 'blocked' yet drops the turn and
        the prompt on reload, since cancellation pre-empts its finalize. The task is
        suspended, so reading its state is race-free. Best-effort: a raising hook must not
        stop the bound. ``message`` is ``str(RunTimeout)``, the single place a bound's
        ``kind`` becomes words."""
        if run.on_timeout is None:
            return
        with suppress(Exception):
            run.on_timeout(message)

    @staticmethod
    def _flush_park_cancel(run: Run) -> None:
        """The pre-cancel flush for a parked run. Its persistence is otherwise deferred to a
        resume (`agent.finalize.finalize`'s parked branch), so cancelling would drop the
        turn and prompt. Called after `run.status` is already `cancelled`, so finalize
        persists instead of wiring resume context. Best-effort: a raising hook must not
        stop the cancel."""
        if run.on_park_cancel is None:
            return
        try:
            run.on_park_cancel()
        except Exception:
            logger.warning("runs: the parked turn of run %s was not written", run.id, exc_info=True)

    @staticmethod
    def _flush_cancel(run: Run) -> None:
        """The pre-cancel flush (as ``_flush_timeout``) before ``task.cancel()``, so a manual
        Stop doesn't drop the turn. Called from this coroutine while the run's task is
        suspended, so reading its state is race-free. It must only persist, never touch
        ``run.status``: ``_execute``'s ``CancelledError`` handler sets ``cancelled`` once the
        cancellation lands. Best-effort: a raising hook must not stop the cancel."""
        if run.on_cancel is None:
            return
        with suppress(Exception):
            run.on_cancel()

    def _fire_terminal(self, run: Run) -> None:
        """Invoke ``on_terminal`` once, synchronously, before the stream closes so it reads an
        accurate ``run.stream.subscriber_count`` (see that property). Exceptions are logged
        and swallowed: they must not affect the already-decided outcome."""
        if self._on_terminal is None:
            return
        try:
            self._on_terminal(run)
        except Exception:
            logger.exception("runs: on_terminal hook failed for run %s", run.id)

    def _evict_old(self) -> None:
        """Bound memory: drop the oldest terminal runs past the retention cap. A run with
        afterwork still going is kept, since it holds the only reference to that work."""
        terminal = [r for r in self._runs.values() if r.is_terminal and not r.afterwork]
        overflow = len(terminal) - self._max_retained
        if overflow <= 0:
            return
        terminal.sort(key=lambda r: r.ended_at or r.created_at)
        for run in terminal[:overflow]:
            _ = self._runs.pop(run.id, None)
