"""Pillar I — the RunRegistry: launch, track, bound, and cancel Runs.

In-process: Runs are asyncio tasks tracked in a dict, gated by a **per-lane**
concurrency semaphore (bursts queue at the gate — the ``queued`` state, which
also prevents overlapping overload). Which lane a run waits in follows from its
``kind`` — see ``runs/lanes.py`` for why the operator's own turn does not share a
pool with unattended work. The registry owns the lifecycle mechanics —
queued→running, the terminal-state mapping, the wall-clock + inactivity bounds,
and cancellation — so every orchestrator inherits them for free.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from contextlib import suppress
from uuid import uuid4

from pydantic import BaseModel

from .events import LimitNotice, RunEnded, RunError, RunMetrics, RunStarted, now_utc
from .lanes import LaneGate, LaneLimits
from .run import Orchestrator, Run, RunStatus
from .stream import RunStream

logger = logging.getLogger(__name__)

_UNSET = object()


class ConversationBusyError(Exception):
    """Raised by ``claim`` or ``submit`` to refuse an operation because a live run — or
    another in-flight claim — already holds ``conversation_id``. The atomic backstop
    behind the route-level 409 guards in ``routes/chat.py``/``routes/conversations.py``
    (``deps.claim_conversation``).

    ``submit``'s own check-and-register runs with no ``await`` in between, so it's
    atomic against a second bare run-creation attempt on its own — but a route whose
    *own* mutation or submit is preceded by a real ``await`` (a model resolve, an
    orphan-attachment lookup) needs more than that: two near-simultaneous requests can
    both observe "no live run" before either does its own mutating work. ``claim``
    closes that gap — it must be taken before the route's first such ``await`` and
    held (released in a ``finally``) through the route's own submit/mutation.
    """

    def __init__(self, conversation_id: str) -> None:
        super().__init__(f"a run is already in progress for conversation {conversation_id!r}")
        self.conversation_id = conversation_id


class RunTimeout(Exception):
    """A Run exceeded a bound. ``kind`` is ``wall_clock`` or ``inactivity``.

    The message is built here from the bound's actual configured duration rather
    than left as the raw enum name — it reaches the operator verbatim, both as the
    toast (``LimitNotice.message``) and the persistent ``blocked`` marker
    (``blockedDetail``), so it must read as a plain sentence, never internal jargon.
    """

    def __init__(self, kind: str, bound_s: float | None = None) -> None:
        super().__init__(_timeout_message(kind, bound_s))
        self.kind = kind


def _fmt_minutes(minutes: float) -> str:
    """The minutes count as a plain string — a whole number when the bound is an
    exact multiple of a minute, else one decimal place. Plain ``round()`` is
    round-half-to-even (``round(2.5) == 2``), which would silently understate (or
    overstate) a fractional operator-configured bound like 150s/2.5 minutes; this
    reports the actual configured duration instead of any rounded approximation."""
    if minutes.is_integer():
        return str(int(minutes))
    return f"{minutes:.1f}"


def _adj_duration(bound_s: float) -> str:
    """A hyphenated adjective phrase for a duration, e.g. ``30-minute``/``45-second``."""
    minutes = bound_s / 60
    if minutes >= 1:
        return f"{_fmt_minutes(minutes)}-minute"
    return f"{max(1, round(bound_s))}-second"


def _count_duration(bound_s: float) -> str:
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
        return f"this run hit the {_adj_duration(bound_s)} overall limit"
    if kind == "inactivity":
        if not bound_s:
            return "no activity for too long"
        return f"no activity for {_count_duration(bound_s)}"
    return f"{kind} timeout exceeded"  # defensive fallback for an unrecognized kind


class RunRegistry:
    def __init__(
        self,
        *,
        lanes: LaneLimits | None = None,
        wall_clock_timeout_s: float | None = None,
        inactivity_timeout_s: float | None = None,
        max_retained: int = 200,
        on_terminal: Callable[[Run], None] | None = None,
    ) -> None:
        self._runs: dict[str, Run] = {}
        self._lanes = LaneGate(lanes)
        self._wall_clock = wall_clock_timeout_s
        self._inactivity = inactivity_timeout_s
        self._max_retained = max_retained
        # (owner_id, conversation_id) pairs a route currently holds via `claim` —
        # in-flight requests that haven't (yet, or ever will) register a Run but must
        # still block a second request from mutating/submitting on the same
        # conversation. See `claim`/`release`.
        self._claims: set[tuple[str, str]] = set()
        # Optional hook fired exactly once per run at its terminal transition (done,
        # blocked, error, or cancelled), *before* the stream closes — the substrate
        # stays decoupled from what a caller does with it (app.py composes the
        # attention-surface emit policy over this; nothing in `runs/` imports
        # `services/`). Synchronous and best-effort: see `_fire_terminal`.
        self._on_terminal = on_terminal

    # --- lookup ---------------------------------------------------------------
    def get(self, run_id: str) -> Run | None:
        return self._runs.get(run_id)

    def list(self, owner_id: str | None = None) -> list[Run]:
        runs = list(self._runs.values())
        if owner_id is not None:
            runs = [r for r in runs if r.owner_id == owner_id]
        return runs

    def active_run_for(self, conversation_id: str, owner_id: str) -> Run | None:
        """The most-recent non-terminal run driving ``conversation_id``, if any.

        How a reattaching client (page reload) maps a conversation back to its
        in-flight run: an in-flight chat turn isn't persisted until it finishes,
        so the conversation read alone can't show a streaming answer — this points
        the client at the run whose events it can replay and resume.
        """
        candidates = [
            r
            for r in self._runs.values()
            if r.owner_id == owner_id and r.conversation_id == conversation_id and not r.is_terminal
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda r: r.created_at)

    # --- claim (pre-submit / pre-mutation mutual exclusion) --------------------
    def claim(self, conversation_id: str, owner_id: str) -> None:
        """Atomically check-and-claim ``conversation_id`` for a request that is about
        to reposition the active leaf (regenerate/edit/rewind/switch-version/a purging
        delete) or submit a run, with further ``await``s of its own still ahead of it.

        Synchronous — no ``await`` between the check and the claim — so under
        single-threaded asyncio two near-simultaneous callers can't both observe "no
        live run, no existing claim" and both proceed: only the first to reach this
        call wins. Raises :class:`ConversationBusyError` when a live (non-terminal) run
        already drives this conversation, or another in-flight request already holds
        the claim.

        The caller **must** release the claim via `release`, in a ``finally`` covering
        every exit path (a failed model resolve, a 404 message id, a successful
        submit) — an unreleased claim wrongly strands the conversation "busy" forever.
        """
        key = (owner_id, conversation_id)
        if key in self._claims or self.active_run_for(conversation_id, owner_id) is not None:
            raise ConversationBusyError(conversation_id)
        self._claims.add(key)

    def release(self, conversation_id: str, owner_id: str) -> None:
        """Release a claim taken by `claim`. Idempotent — safe even if `claim` raised
        (or was never called) for this pair."""
        self._claims.discard((owner_id, conversation_id))

    # --- launch ---------------------------------------------------------------
    def submit(
        self,
        *,
        kind: str,
        owner_id: str,
        orchestrator: Orchestrator,
        run_id: str | None = None,
        conversation_id: str | None = None,
        wall_clock_timeout_s: float | None | object = _UNSET,
        inactivity_timeout_s: float | None | object = _UNSET,
    ) -> Run:
        # Atomic check-and-claim: reads ``self._runs`` and registers the new run with no
        # ``await`` in between, so this closes the race a caller's own pre-submit guard
        # can't — two requests that both saw no active run can still both reach here, and
        # only the one that actually runs first wins the slot.
        if conversation_id is not None and (
            self.active_run_for(conversation_id, owner_id) is not None
        ):
            raise ConversationBusyError(conversation_id)
        run = Run(
            id=run_id or uuid4().hex,
            kind=kind,
            owner_id=owner_id,
            conversation_id=conversation_id,
            stream=RunStream(),
        )
        self._runs[run.id] = run
        self._evict_old()
        wall = self._wall_clock if wall_clock_timeout_s is _UNSET else wall_clock_timeout_s
        idle = self._inactivity if inactivity_timeout_s is _UNSET else inactivity_timeout_s
        run.task = asyncio.create_task(
            self._execute(run, orchestrator, wall, idle),  # type: ignore[arg-type]
            name=f"run:{run.id}",
        )
        return run

    async def cancel(self, run_id: str) -> bool:
        """Request cancellation.

        Two mechanisms exist, both armed here: ``run.cancel_requested`` is a
        cooperative flag the orchestrator's own loop checks at its next step
        boundary (see ``agent/turn.py``'s ``chat-05`` wiring); ``task.cancel()``
        below is the immediate hard backstop that fires regardless.

        Idempotent against a repeated cancel on the same run before the first
        cancellation actually lands: a running (not yet terminal, not parked) run
        stays ``running`` until its ``CancelledError`` is delivered on a later
        event-loop tick, so a second call in that window must not re-flush or
        re-persist — ``run.cancel_requested`` already being set is the signal
        that a cancel is already in flight for this run.
        """
        run = self._runs.get(run_id)
        if run is None or run.is_terminal:
            return False
        if run.status is RunStatus.awaiting_input:
            # Parked: the task already ended, so there is nothing to interrupt — but
            # unlike a running turn, a parked turn's own persistence is otherwise only
            # ever recorded on resume, so flush it here first (see `_flush_park_cancel`)
            # before finalizing and closing the stream, or the operator's own prompt
            # (and any tool calls already completed before the deferred one) is
            # silently dropped.
            run.cancel_requested = True
            run.status = RunStatus.cancelled
            self._flush_park_cancel(run)
            run.emit(RunEnded(outcome="cancelled"))
            run.ended_at = now_utc()
            self._fire_terminal(run)
            run.stream.close()
            # "The task already ended" holds for a settled park, but not for the window
            # between `park()` and the orchestrator's own return — it still has work to
            # unwind (settling the concurrent namer). Interrupt it, or it resumes into a
            # run this call has already finalized.
            if run.task is not None and not run.task.done():
                run.task.cancel()
            return True
        if run.cancel_requested:
            # Already flushed and hard-cancelled by an earlier call; the task
            # simply hasn't unwound to a terminal status yet. Report success
            # without repeating the (non-idempotent) flush/persist side effect.
            return True
        run.cancel_requested = True
        self._flush_cancel(run)
        if run.task is not None:
            run.task.cancel()
        return True

    async def shutdown(self, timeout_s: float = 5.0) -> None:
        """Cancel every live run and wait up to ``timeout_s`` for them to unwind.

        The app registers this **last** in its lifecycle, so it stops **first**: an
        orchestrator writes through stores (the conversation write-behind drainer above
        all) that shutdown is otherwise free to tear down under it, and a submit onto a
        stopped drainer is discarded with no error and no ``on_drop``. Cancelling here
        runs each turn's own pre-cancel flush while the stores are still live, so a turn
        in flight at shutdown is persisted rather than silently lost.
        """
        live = [run for run in self._runs.values() if not run.is_terminal]
        for run in live:
            await self.cancel(run.id)
        pending = [run.task for run in live if run.task is not None and not run.task.done()]
        if pending:
            # Bounded: a turn's own pre-cancel flush is worth waiting for, a tool that
            # swallows the cancel is not worth a shutdown that never ends.
            _, stuck = await asyncio.wait(pending, timeout=timeout_s)
            if stuck:
                logger.warning("runs: %d run(s) still unwinding after %.1fs", len(stuck), timeout_s)

    async def resume(
        self,
        run_id: str,
        orchestrator: Orchestrator,
        *,
        wall_clock_timeout_s: float | None | object = _UNSET,
        inactivity_timeout_s: float | None | object = _UNSET,
    ) -> Run | None:
        """Continue a parked run with a fresh orchestrator (approval resume).

        Runs on the same Run/stream — no new ``run.started``, no replayed
        history — and inherits fresh bounds for the continuation turn.
        """
        run = self._runs.get(run_id)
        if run is None or run.status is not RunStatus.awaiting_input:
            return None
        # Let the parked attempt's task fully exit before we touch status —
        # otherwise it could wake, see status flipped, and wrongly finalize.
        if run.task is not None:
            with suppress(asyncio.CancelledError):
                await run.task
        if run.status is not RunStatus.awaiting_input:
            return None  # cancelled (or already resumed) while we waited
        run.status = RunStatus.queued
        wall = self._wall_clock if wall_clock_timeout_s is _UNSET else wall_clock_timeout_s
        idle = self._inactivity if inactivity_timeout_s is _UNSET else inactivity_timeout_s
        run.task = asyncio.create_task(
            self._execute(run, orchestrator, wall, idle, resuming=True),  # type: ignore[arg-type]
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
            # Bursts wait here while ``queued`` — bounded concurrency, in this run's own
            # lane, so a saturated lane never holds up work in another one.
            async with self._lanes.slot(run.kind):
                run.status = RunStatus.running
                run.touch()
                if not resuming:
                    run.started_at = now_utc()
                    run.emit(RunStarted(run_id=run.id, kind=run.kind))
                await self._supervise(run, orchestrator, wall_clock, inactivity)
                if run.status is RunStatus.awaiting_input:
                    # Parked for approval: leave the stream open and the
                    # slot free; the run is resumed or cancelled out of band.
                    return
                if not run.is_terminal:
                    run.status = RunStatus.done
                self._emit(run, run.metrics or RunMetrics())
                self._emit(run, RunEnded(outcome=run.status.value, detail=run.detail))
        except RunTimeout as timeout:
            # Terminate the same way a usage-limit stop does (LimitNotice + a
            # blocked RunEnded), not RunError — a timeout is an expected bound, not
            # a failure, and the frontend already renders limit.notice as a toast
            # and a blocked outcome as a persistent inline marker.
            self._emit(run, LimitNotice(limit="time", message=str(timeout)))
            parked = run.status is RunStatus.awaiting_input
            run.block(str(timeout))
            if parked:
                # The bound tripped in the window after the orchestrator parked but
                # before its task finished unwinding. A parked turn's persistence is
                # otherwise deferred to the resume that will now never happen, and
                # `on_timeout` was already disarmed at the park — so without this the
                # whole turn (the operator's own prompt included) is silently dropped.
                self._flush_park_cancel(run)
            self._emit(run, run.metrics or RunMetrics())
            self._emit(run, RunEnded(outcome=run.status.value, detail=run.detail))
        except asyncio.CancelledError:
            # The Run's own top-level handler turns cancellation into a recorded
            # terminal state rather than propagating it — intentional.
            run.status = RunStatus.cancelled
            # Metrics precede every closing frame, on every path. A stopped or failed
            # turn still spent the tokens it spent, and a usage view that silently
            # omits exactly the runs that went wrong is worse than no view.
            self._emit(run, run.metrics or RunMetrics())
            self._emit(run, RunEnded(outcome="cancelled"))
        except Exception as exc:  # noqa: BLE001 — orchestrator failures are terminal, not fatal
            run.status = RunStatus.error
            run.error = str(exc)
            self._emit(run, run.metrics or RunMetrics())
            self._emit(run, RunError(message=str(exc), kind=type(exc).__name__))
        finally:
            # Only finalize on a terminal outcome — a parked run keeps its
            # stream open for the eventual resume. A closed stream means a
            # concurrent `cancel` already finalized this run (see `_emit`); its
            # outcome and its terminal hook are recorded, so don't fire either twice.
            if run.is_terminal and not run.stream.closed:
                run.ended_at = run.ended_at or now_utc()
                self._fire_terminal(run)
                run.stream.close()

    @staticmethod
    def _emit(run: Run, body: BaseModel) -> None:
        """Emit a lifecycle frame unless the stream is already closed.

        A parked run stays externally visible while its task is still unwinding (the
        orchestrator has more to do after ``park`` — settling the concurrent namer, for
        one), so `cancel`'s parked branch can close the stream out from under this
        coroutine. Emitting onto it would raise, flip the recorded ``cancelled`` outcome
        to ``error``, and fire the terminal hook a second time. The cancel already
        recorded the outcome; there is nothing left for these frames to say.
        """
        if not run.stream.closed:
            run.emit(body)

    async def _supervise(
        self,
        run: Run,
        orchestrator: Orchestrator,
        wall_clock: float | None,
        inactivity: float | None,
    ) -> None:
        """Run the orchestrator under wall-clock + inactivity bounds."""
        # Tell the run which bound it is being held to, so `Run.keepalive` can beat inside
        # it rather than against a fixed interval that a shortened timeout would outrun.
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
                main.cancel()
                with suppress(asyncio.CancelledError):
                    await main

    @staticmethod
    def _flush_timeout(run: Run, message: str) -> None:
        """Give the orchestrator one last chance to persist its partial state before
        the bound trips and ``_supervise``'s ``finally`` force-cancels its task —
        without this, a wall-clock/inactivity stop reports a 'blocked' outcome that
        looks persistent but silently drops the turn (and the operator's own prompt)
        on the next reload, because cancellation interrupts the task before its own
        normal finalize path runs. Called while the task is still suspended (not
        running), so reading its state here is race-free under single-threaded
        asyncio. Best-effort: a hook that raises must not stop the bound from firing.
        ``message`` is the same operator-legible sentence (``str(RunTimeout)``, built
        from the bound's configured duration) used for the toast/persisted marker, so
        there is exactly one place that turns a bound's ``kind`` into words."""
        if run.on_timeout is None:
            return
        with suppress(Exception):
            run.on_timeout(message)

    @staticmethod
    def _flush_park_cancel(run: Run) -> None:
        """Give a parked (awaiting-approval) run the same pre-cancel flush opportunity
        `_flush_cancel` gives a still-running turn. A parked turn's task has already
        exited, so there is nothing to interrupt — but its persistence is otherwise
        deferred until the eventual resume (see `agent.finalize.finalize`'s parked
        branch), so without this hook, cancelling it instead of resuming it silently
        drops the whole parked turn (the operator's own prompt included) on the next
        reload. Called after `run.status` has already been set to the terminal
        `cancelled` value, so the orchestrator's own finalize path persists rather than
        re-wiring resume context for a run that is no longer going to resume.
        Best-effort: a hook that raises must not stop the cancel from proceeding."""
        if run.on_park_cancel is None:
            return
        with suppress(Exception):
            run.on_park_cancel()

    @staticmethod
    def _flush_cancel(run: Run) -> None:
        """Give the orchestrator the same pre-cancel flush opportunity ``_flush_timeout``
        gives the wall-clock/inactivity bounds — before ``task.cancel()`` force-unwinds it,
        so a manual Stop doesn't drop the turn (and the operator's own prompt) on the next
        reload the way an un-flushed timeout would (see ``_flush_timeout``). Called from
        this coroutine — not the run's own task — while that task is provably suspended:
        under single-threaded asyncio nothing else runs while this does, so reading the
        task's state here is race-free. Deliberately does not touch ``run.status``/outcome:
        ``_execute``'s own ``except asyncio.CancelledError`` handler sets the terminal
        ``cancelled`` status once the cancellation actually lands, so this hook must only
        persist, never finalize the run's terminal state. Best-effort: a hook that raises
        must not stop the cancel from proceeding."""
        if run.on_cancel is None:
            return
        with suppress(Exception):
            run.on_cancel()

    def _fire_terminal(self, run: Run) -> None:
        """Invoke the injected ``on_terminal`` hook exactly once, synchronously, right
        before the stream closes — so it can still read an accurate
        ``run.stream.subscriber_count`` (see that property's docstring for why after
        ``close()`` would race the subscribers' own cleanup). Swallows (and logs) any
        exception: a notification failure must never affect a run's own recorded
        outcome, which has already been decided by the time this fires."""
        if self._on_terminal is None:
            return
        try:
            self._on_terminal(run)
        except Exception:
            logger.exception("runs: on_terminal hook failed for run %s", run.id)

    def _evict_old(self) -> None:
        """Bound memory: drop the oldest terminal runs past the retention cap."""
        terminal = [r for r in self._runs.values() if r.is_terminal]
        overflow = len(terminal) - self._max_retained
        if overflow <= 0:
            return
        terminal.sort(key=lambda r: r.ended_at or r.created_at)
        for run in terminal[:overflow]:
            self._runs.pop(run.id, None)
