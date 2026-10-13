"""The task scheduler — the in-process tick loop behind `ScheduledTask` (`TASK-1..6`).

Single-instance, in-process: one asyncio task sleeps until the earliest
`next_run_at`, wakes, fires whatever is due, and sleeps again. It owns exactly the
scheduling mechanics — due-detection, non-overlap, lock-awareness, and recording each
firing as a `TaskRun` — and knows nothing about *how* a task actually runs: an agent
task is driven by an injected `executor` callable, a reminder by an injected `notify`
callable. Composing those two into "an agent task becomes an ordinary Run in a fresh
conversation" / "a reminder becomes a notification" is the caller's job (`app.py`,
wired by a later phase) — this module stays reusable by anything that can produce a
`TaskRunResult` for a `ScheduledTaskView`.

**Lock-aware** like `core.worker.WriteBehindWorker`: task prompts are encrypted, so
the loop parks whenever the vault is locked rather than erroring, and resumes on unlock.
A locked/parked scheduler (or a long boot) can leave a task's `next_run_at` in the past;
the due-check doubles as the catch-up path, firing an overdue task **once** and counting
its next occurrence from *now*, so a gap never floods a burst of missed fires.

**Every occurrence fires on time.** A due fire first advances the task's schedule — that
write is what claims the occurrence, so no later tick can fire it again — and then starts
its run. A run of the same task still going (parked on an approval, say) does not hold
the next one back: each occurrence is its own run.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from croniter import croniter
from sqlalchemy import Engine
from sqlalchemy.exc import OperationalError
from sqlmodel import Session, select

from core.db import in_session
from core.serde import as_utc
from core.tasks import STOP_TIMEOUT_S, cancel_and_wait
from core.vault import Vault, VaultLocked
from core.worker import wait_unlocked_or_stop
from models._fields import new_id, utcnow
from models.task import ScheduledTask, ScheduleType, TaskKind, TaskOutcome, TaskRun

logger = logging.getLogger(__name__)

# Passed as `next_run_at` to leave the stored value as it is.
_KEEP = object()

# The tick loop's own floor on how soon it re-checks after finding something already
# due — see `_wait_for`.
_MIN_POLL_S = 0.05

# Bounded retry for SQLite's transient "database is locked"/"database is busy" on the
# scheduler's own bookkeeping writes (`_retry_locked`). `core.db` sets a busy_timeout
# so a colliding write normally just waits the lock out, but a wait can still lose
# under pathological contention, and an escaped advance delays a due fire to a later
# tick. Bounded so a genuinely broken database still surfaces instead of looping forever.
_DB_LOCK_RETRIES = 5
_DB_LOCK_RETRY_BASE_S = 0.05
_DB_LOCK_RETRY_MAX_S = 0.5

# How long shutdown waits for in-flight executions to finalize their own `TaskRun` rows
# before giving up on them. Generous enough for a turn to flush, short enough that one
# wedged execution can't stop the process from exiting.
_SHUTDOWN_DRAIN_TIMEOUT_S = 30.0


def _is_transient_lock(exc: OperationalError) -> bool:
    """A SQLite "busy" — another connection holds the write lock *right now* — as
    opposed to a genuinely broken database (missing table, corrupt file), which no
    amount of retrying will fix."""
    message = str(exc.orig if exc.orig is not None else exc).lower()
    return "database is locked" in message or "database is busy" in message


@dataclass(frozen=True)
class ScheduledTaskView:
    """A task's decrypted content, plus the metadata an executor/notifier needs —
    the shape passed into the two injected callables. Never persisted itself."""

    id: str
    owner_id: str
    kind: str  # TaskKind
    title: str
    prompt: str
    output: str  # TaskOutput
    pre_authorized: list[str]
    agent_name: str | None = None


@dataclass(frozen=True)
class TaskRunResult:
    """What an ``executor`` reports back for one agent-task execution. A ``notify``
    call for a reminder never produces one of these — the scheduler synthesizes an
    ``ok`` outcome itself unless ``notify`` raises."""

    outcome: str  # TaskOutcome
    run_id: str | None = None
    conversation_id: str | None = None
    summary: str | None = None


TaskExecutor = Callable[[ScheduledTaskView], Awaitable[TaskRunResult]]
TaskNotifier = Callable[[ScheduledTaskView], Awaitable[None]]


def compute_next_run(
    schedule_type: str,
    *,
    anchor: datetime,
    run_at: datetime | None = None,
    every_seconds: float | None = None,
    cron_expr: str | None = None,
    timezone: str = "UTC",
) -> datetime | None:
    """The next `next_run_at` for a schedule, given an anchor (the fire time it's
    counted forward from, for interval/cron — "anchored on last fire"). A cron's fields
    are read in ``timezone``, so "9am on weekdays" stays 9am across DST. Deterministic
    and side-effect-free so both the scheduler and the future task-creation route can
    share it. Degrades to `None` ("never due") for a malformed schedule (missing
    interval, missing cron) rather than raising — the write-side route is where a bad
    schedule gets rejected, not this always-safe read path."""
    if schedule_type == ScheduleType.ONCE:
        # The single fire time itself; the caller disables the task once it has
        # actually fired rather than looping this function again.
        return run_at
    if schedule_type == ScheduleType.INTERVAL:
        if every_seconds is None or every_seconds <= 0:
            return None
        return anchor + timedelta(seconds=every_seconds)
    if schedule_type == ScheduleType.CRON:
        if not cron_expr:
            return None
        local = anchor.astimezone(ZoneInfo(timezone))
        return croniter(cron_expr, local).get_next(datetime).astimezone(UTC)
    return None  # ScheduleType.WEBHOOK (or an unrecognized value) — never time-due


class SchedulerService:
    def __init__(
        self,
        engine: Engine,
        vault: Vault,
        executor: TaskExecutor,
        notify: TaskNotifier,
    ) -> None:
        self._engine = engine
        self._vault = vault
        self._executor = executor
        self._notify = notify
        # `TaskRun` id → the execution recording it.
        self._running: dict[str, asyncio.Task] = {}
        self._wake = asyncio.Event()
        self._stopping = asyncio.Event()
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._stopping.clear()
        self._task = asyncio.create_task(self._run(), name="scheduler")

    async def stop(self) -> None:
        """Stop the tick loop and let any in-flight executions finish naturally —
        each one is responsible for its own `TaskRun` finalize, so cutting them off
        would strand a row `finished_at`-less forever."""
        self._stopping.set()
        self._wake.set()  # unstick a park on `_wait_unlocked`/`_wait_for`
        await cancel_and_wait(self._task, STOP_TIMEOUT_S)
        self._task = None
        if self._running:
            # Bounded: an execution that will not settle must not take the whole shutdown
            # with it. Letting one finalize its own `TaskRun` row is worth waiting for;
            # waiting forever means every lifecycle unit registered before this one never
            # gets to stop either, and the process hangs instead of exiting.
            try:
                await asyncio.wait_for(
                    asyncio.gather(*list(self._running.values()), return_exceptions=True),
                    timeout=_SHUTDOWN_DRAIN_TIMEOUT_S,
                )
            except TimeoutError:
                logger.warning(
                    "scheduler: %d task execution(s) did not settle within %.0fs; "
                    "their TaskRun rows stay unfinished",
                    len(self._running),
                    _SHUTDOWN_DRAIN_TIMEOUT_S,
                )

    def wake(self) -> None:
        """Re-arm the sleep — call after any task create/update/delete so a changed
        `next_run_at` is picked up immediately rather than waiting out whatever the
        loop was already sleeping toward."""
        self._wake.set()

    # --- the tick loop ----------------------------------------------------------

    async def _run(self) -> None:
        while not self._stopping.is_set():
            if not await self._wait_unlocked():
                return
            now = utcnow()
            await self._tick(now)
            sleep_for = await self._sleep_duration()
            await self._wait_for(sleep_for)

    async def _wait_unlocked(self) -> bool:
        """Park while the vault is locked (task prompts are encrypted). Returns False only
        if stopping while still locked — the loop should simply exit, not tick against an
        unreadable vault."""
        return await wait_unlocked_or_stop(self._vault.unlocked_event, self._stopping)

    async def _wait_for(self, seconds: float | None) -> None:
        """Sleep until the earliest due task, a wake, or a stop — whichever first.
        `seconds is None` means "nothing scheduled": wait indefinitely for a wake/stop.
        A non-positive (or tiny) `seconds` is floored to `_MIN_POLL_S` rather than
        producing a zero-wait re-tick, so a task whose schedule advance just failed is
        retried at a bounded rate rather than in a busy loop."""
        if self._stopping.is_set():
            self._wake.clear()
            return
        stop_wait = asyncio.ensure_future(self._stopping.wait())
        wake_wait = asyncio.ensure_future(self._wake.wait())
        waits = {stop_wait, wake_wait}
        if seconds is not None:
            waits.add(asyncio.ensure_future(asyncio.sleep(max(seconds, _MIN_POLL_S))))
        try:
            await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for fut in waits:
                fut.cancel()
            await asyncio.gather(*waits, return_exceptions=True)
        self._wake.clear()

    async def _sleep_duration(self) -> float | None:
        earliest = await self._earliest_next_run()
        if earliest is None:
            return None
        return (earliest - utcnow()).total_seconds()

    async def _tick(self, now: datetime) -> None:
        for task in await self._load_due(now):
            await self._fire_due(task, now)

    async def _fire_due(self, task: ScheduledTask, now: datetime) -> None:
        """Claim this occurrence by advancing the schedule, then start its run. A failed
        advance leaves the task due, so the next tick tries again rather than firing an
        occurrence nothing has claimed."""
        try:
            if task.schedule_type == ScheduleType.ONCE:
                await self._advance(task.id, next_run_at=None, enabled=False)
            else:
                next_run = compute_next_run(
                    task.schedule_type,
                    anchor=now,
                    run_at=task.run_at,
                    every_seconds=task.every_seconds,
                    cron_expr=task.cron_expr,
                    timezone=task.timezone,
                )
                await self._advance(task.id, next_run_at=next_run)
        except Exception:  # noqa: BLE001 — a failed reschedule must never kill the loop
            logger.exception("scheduler: task %s schedule advance failed", task.id)
            return
        self._start(task, now)

    # --- on-demand fire (manual run_now / an inbound webhook) ----------------------

    async def fire_now(self, task_id: str) -> str | None:
        """Fire ``task_id`` immediately, leaving its schedule as it is — the manual
        ``run_now`` and webhook entry point. Returns the id of the `TaskRun` it records,
        or ``None`` if the task does not exist, is disabled, or awaits approval: an
        operator turning a task off stops its webhook too, not only its schedule."""

        def work(session: Session) -> ScheduledTask | None:
            return session.get(ScheduledTask, task_id)

        task = await in_session(self._engine, work)
        if task is None or not task.enabled or not task.approved:
            return None
        return self._start(task, utcnow())

    # --- firing ---------------------------------------------------------------------

    def _start(self, task: ScheduledTask, fire_time: datetime) -> str:
        run_row_id = new_id()
        execution = asyncio.create_task(
            self._fire(task, fire_time, run_row_id), name=f"scheduled-task:{task.id}"
        )
        self._running[run_row_id] = execution
        execution.add_done_callback(lambda _: self._running.pop(run_row_id, None))
        return run_row_id

    async def _fire(self, task: ScheduledTask, fire_time: datetime, run_row_id: str) -> None:
        await self._insert_task_run(id=run_row_id, task_id=task.id, started_at=fire_time)
        try:
            view = self._decrypt_view(task)
            if task.kind == TaskKind.REMINDER:
                await self._notify(view)
                result = TaskRunResult(outcome=TaskOutcome.OK.value)
            else:
                result = await self._executor(view)
        except Exception as exc:  # noqa: BLE001 — a bad task must never kill the loop
            logger.exception("scheduler: task %s execution failed", task.id)
            result = TaskRunResult(outcome=TaskOutcome.ERROR.value, summary=str(exc))
        finished = utcnow()
        if not await self._finalize_parked(
            run_row_id,
            finished_at=finished,
            outcome=result.outcome,
            run_id=result.run_id,
            conversation_id=result.conversation_id,
            summary=result.summary,
        ):
            # Stopping while the vault stayed locked through the whole park: the row
            # stays unfinalized, like `WriteBehindWorker.stop()` leaving a locked queue.
            return
        # Bookkeeping only; a failure here must never kill the loop.
        try:
            await self._advance(task.id, last_run_at=finished)
        except Exception:
            logger.exception("scheduler: task %s last-run stamp failed", task.id)

    def _decrypt_view(self, task: ScheduledTask) -> ScheduledTaskView:
        return ScheduledTaskView(
            id=task.id,
            owner_id=task.owner_id,
            kind=task.kind,
            title=self._vault.decrypt_str(task.title_enc),
            prompt=self._vault.decrypt_str(task.prompt_enc),
            output=task.output,
            pre_authorized=list(task.pre_authorized),
            agent_name=task.agent_name,
        )

    # --- persistence --------------------------------------------------------------

    async def _load_due(self, now: datetime) -> list[ScheduledTask]:
        # The due check itself runs in Python (not a SQL `WHERE next_run_at <= :now`)
        # so the naive round-trip `as_utc` corrects for can't produce a wrong
        # comparison at the SQL layer — the same reasoning `approval_grants.py`
        # documents for its own expiry check.
        def work(session: Session) -> list[ScheduledTask]:
            return list(
                session.exec(
                    select(ScheduledTask)
                    .where(ScheduledTask.enabled.is_(True))
                    .where(ScheduledTask.approved.is_(True))
                    .where(ScheduledTask.next_run_at.is_not(None))
                ).all()
            )

        rows = await in_session(self._engine, work)
        # `next_run_at` is nullable on the model and only the SQL filter above excludes
        # the nulls, which no reader of this expression can see — so it is re-checked
        # here rather than assumed.
        return [
            row for row in rows if row.next_run_at is not None and as_utc(row.next_run_at) <= now
        ]

    async def _earliest_next_run(self) -> datetime | None:
        def work(session: Session) -> list[datetime | None]:
            return list(
                session.exec(
                    select(ScheduledTask.next_run_at)
                    .where(ScheduledTask.enabled.is_(True))
                    .where(ScheduledTask.approved.is_(True))
                    .where(ScheduledTask.next_run_at.is_not(None))
                ).all()
            )

        rows = await in_session(self._engine, work)
        return min((as_utc(at) for at in rows if at is not None), default=None)

    async def _insert_task_run(self, **kwargs) -> None:
        def work(session: Session) -> None:
            session.add(TaskRun(**kwargs))

        await in_session(self._engine, work)

    async def _finalize_parked(
        self,
        run_row_id: str,
        *,
        finished_at: datetime,
        outcome: str,
        run_id: str | None,
        conversation_id: str | None,
        summary: str | None,
    ) -> bool:
        """Finalize a `TaskRun`, parking and retrying if the vault locks mid-flight
        (the operator locks it while an agent task is still executing, so
        `encrypt_str` on the summary raises `VaultLocked` after the executor has
        already returned) — mirrors `WriteBehindWorker`'s "a lock landing
        mid-handler is a park, not a failed attempt" rule rather than letting the
        exception escape the bare `asyncio.create_task` in `_tick` and strand this
        run's row and the task's own reschedule forever. A transient locked
        database is ridden out the same way (`_retry_locked`) — bounded rather than
        parked, since unlike a vault lock nothing signals when it clears. Returns
        False only if the loop is stopping while the vault never unlocked again —
        same as `WriteBehindWorker.stop()` leaving a locked queue's items
        unflushed."""
        while True:
            try:
                await self._retry_locked(
                    lambda: self._finalize_task_run(
                        run_row_id,
                        finished_at=finished_at,
                        outcome=outcome,
                        run_id=run_id,
                        conversation_id=conversation_id,
                        summary=summary,
                    ),
                    what=f"task run {run_row_id} finalize",
                )
                return True
            except VaultLocked:
                logger.warning(
                    "scheduler: vault locked mid-finalize for task run %s — parking",
                    run_row_id,
                )
                if not await self._wait_unlocked():
                    return False

    async def _finalize_task_run(
        self,
        run_row_id: str,
        *,
        finished_at: datetime,
        outcome: str,
        run_id: str | None,
        conversation_id: str | None,
        summary: str | None,
    ) -> None:
        summary_enc = self._vault.encrypt_str(summary) if summary is not None else None

        def work(session: Session) -> None:
            row = session.get(TaskRun, run_row_id)
            if row is None:
                return
            row.finished_at = finished_at
            row.outcome = outcome
            row.run_id = run_id
            row.conversation_id = conversation_id
            row.summary_enc = summary_enc
            session.add(row)

        await in_session(self._engine, work)

    async def _retry_locked(self, attempt: Callable[[], Awaitable[None]], *, what: str) -> None:
        """Run a DB write, riding out SQLite's transient `database is locked`/`busy`
        (another connection — typically a write-behind drainer mid-flush — holds the
        single write lock). `core.db`'s busy_timeout makes this rare, but not
        impossible, and the scheduler's bookkeeping writes must not escape on a moment's
        contention. Bounded with backoff so a genuinely permanent error (or a lock that
        outlives every attempt) still surfaces rather than looping."""
        delay = _DB_LOCK_RETRY_BASE_S
        for attempts_left in range(_DB_LOCK_RETRIES - 1, -1, -1):
            try:
                return await attempt()
            except OperationalError as exc:
                if not _is_transient_lock(exc):
                    raise
                if attempts_left == 0:
                    logger.error(
                        "scheduler: %s still hitting a locked database after %d attempts"
                        " — giving up",
                        what,
                        _DB_LOCK_RETRIES,
                    )
                    raise
                logger.warning(
                    "scheduler: %s hit a locked database — retrying in %.2fs", what, delay
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, _DB_LOCK_RETRY_MAX_S)

    async def _advance(
        self,
        task_id: str,
        *,
        next_run_at: datetime | None | object = _KEEP,
        last_run_at: datetime | None = None,
        enabled: bool | None = None,
    ) -> None:
        """Update a task's schedule row, riding out a transient locked database. Moving
        `next_run_at` is what claims an occurrence before it fires, so a moment's lock
        contention must not push the fire to a later tick."""
        await self._retry_locked(
            lambda: self._advance_once(
                task_id, next_run_at=next_run_at, last_run_at=last_run_at, enabled=enabled
            ),
            what=f"task {task_id} schedule advance",
        )

    async def _advance_once(
        self,
        task_id: str,
        *,
        next_run_at: datetime | None | object = _KEEP,
        last_run_at: datetime | None = None,
        enabled: bool | None = None,
    ) -> None:
        def work(session: Session) -> None:
            row = session.get(ScheduledTask, task_id)
            if row is None:
                return
            if next_run_at is not _KEEP:
                row.next_run_at = next_run_at
            if last_run_at is not None:
                row.last_run_at = last_run_at
            if enabled is not None:
                row.enabled = enabled
            row.updated_at = utcnow()
            session.add(row)

        await in_session(self._engine, work)
