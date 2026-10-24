"""The task scheduler: the in-process tick loop behind `ScheduledTask`.

One asyncio task sleeps until the earliest `next_run_at`, fires whatever is due, and sleeps
again. It owns the scheduling mechanics (due-detection, lock-awareness, recording each
firing as a `TaskRun`) and not how a task runs: an agent task goes to an injected
`executor`, a reminder to an injected `notify`, composed by the caller (`app.py`).

**Lock-aware** like `core.worker.WriteBehindWorker`: task prompts are encrypted, so the
loop parks while the vault is locked and resumes on unlock. A park or long boot can leave
`next_run_at` in the past; the due-check doubles as catch-up, firing an overdue task once
and counting its next occurrence from now, so a gap never floods missed fires.

**Every occurrence fires on time.** A due fire first advances the schedule, which claims
the occurrence so no later tick fires it again, then starts its run. A still-running
earlier run (parked on an approval, say) does not hold the next one back.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import Enum
from zoneinfo import ZoneInfo

from croniter import croniter
from sqlalchemy import Engine
from sqlalchemy.exc import OperationalError
from sqlmodel import Session, col, select

from core.db import in_session
from core.serde import as_utc
from core.tasks import STOP_TIMEOUT_S, cancel_and_wait
from core.vault import Vault, VaultLocked
from core.worker import wait_unlocked_or_stop
from models._fields import new_id, utcnow
from models.task import ScheduledTask, ScheduleType, TaskKind, TaskOutcome, TaskRun

logger = logging.getLogger(__name__)


class _Keep(Enum):
    KEEP = object()


# Passed as `next_run_at` to leave the stored value as it is.
_KEEP = _Keep.KEEP

# Floor on how soon the loop re-checks after finding something already due (`_wait_for`).
_MIN_POLL_S = 0.05

# Bounded retry for SQLite's transient "database is locked"/"busy" on the scheduler's
# bookkeeping writes (`_retry_locked`). `core.db`'s busy_timeout usually suffices, but an
# escaped advance delays a due fire; bounded so a broken database still surfaces.
DB_LOCK_RETRIES = 5
_DB_LOCK_RETRY_BASE_S = 0.05
_DB_LOCK_RETRY_MAX_S = 0.5

# How long shutdown waits for in-flight executions to finalize their `TaskRun` rows, so
# one wedged execution cannot stop the process from exiting.
_SHUTDOWN_DRAIN_TIMEOUT_S = 30.0


def _is_transient_lock(exc: OperationalError) -> bool:
    """A SQLite "busy" (another connection holds the write lock right now), as opposed to
    a broken database that retrying will not fix."""
    message = str(exc.orig if exc.orig is not None else exc).lower()
    return "database is locked" in message or "database is busy" in message


@dataclass(frozen=True)
class ScheduledTaskView:
    """A task's decrypted content plus the metadata an executor/notifier needs; never
    persisted."""

    id: str
    owner_id: str
    kind: str  # TaskKind
    title: str
    prompt: str
    output: str  # TaskOutput
    pre_authorized: list[str]
    agent_name: str | None = None
    #: The open `TaskRun` this fire records. An executor that starts a run links it with
    #: `SchedulerService.link_task_run` so the row can be found again after a restart.
    task_run_id: str | None = None


class TaskRunSuspended(Exception):
    """Raised by an executor whose run outlives this process (parked on the operator at
    shutdown). The `TaskRun` stays open: whoever settles the restored run finalizes it."""


@dataclass(frozen=True)
class TaskRunResult:
    """What an ``executor`` reports for one agent-task execution. A reminder's ``notify``
    produces none; the scheduler records ``ok`` unless ``notify`` raises."""

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
    """The next `next_run_at` for a schedule; ``anchor`` is the fire time interval and cron
    count forward from. A cron's fields are read in ``timezone``, so "9am on weekdays"
    stays 9am across DST. Pure. A malformed schedule gives `None` ("never due") rather than
    raising; the write-side route rejects bad schedules."""
    if schedule_type == ScheduleType.ONCE:
        # The caller disables the task once it has fired.
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
    return None  # a webhook (or unrecognized value) is never time-due


class SchedulerService:
    def __init__(
        self,
        engine: Engine,
        vault: Vault,
        executor: TaskExecutor,
        notify: TaskNotifier,
    ) -> None:
        self._engine: Engine = engine
        self._vault: Vault = vault
        self._executor: TaskExecutor = executor
        self._notify: TaskNotifier = notify
        # `TaskRun` id → the execution recording it.
        self._running: dict[str, asyncio.Task[None]] = {}
        self._wake: asyncio.Event = asyncio.Event()
        self._stopping: asyncio.Event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    @property
    def executor(self) -> TaskExecutor:
        """The callable that runs a due task."""
        return self._executor

    @executor.setter
    def executor(self, value: TaskExecutor) -> None:
        self._executor = value

    @property
    def running(self) -> dict[str, asyncio.Task[None]]:
        """In-flight executions keyed by their `TaskRun` id."""
        return self._running

    async def start(self) -> None:
        self._stopping.clear()
        self._task = asyncio.create_task(self._run(), name="scheduler")

    async def stop(self) -> None:
        """Stop the tick loop and let in-flight executions finish: each finalizes its own
        `TaskRun`, so cutting them off would strand a row without `finished_at`. A run
        parked on the operator ends with `TaskRunSuspended` and leaves its row open."""
        self._stopping.set()
        self._wake.set()  # unstick a park on `_wait_unlocked`/`_wait_for`
        await cancel_and_wait(self._task, STOP_TIMEOUT_S)
        self._task = None
        if self._running:
            # Bounded, or an unsettled execution would stop earlier lifecycle units from
            # stopping and hang the process.
            try:
                _ = await asyncio.wait_for(
                    asyncio.gather(*list(self._running.values()), return_exceptions=True),
                    timeout=_SHUTDOWN_DRAIN_TIMEOUT_S,
                )
            except TimeoutError:
                logger.warning(
                    "scheduler: %d task execution(s) did not settle within %.0fs; "
                    + "their TaskRun rows stay unfinished",
                    len(self._running),
                    _SHUTDOWN_DRAIN_TIMEOUT_S,
                )

    def wake(self) -> None:
        """Re-arm the sleep; call after any task create/update/delete so a changed
        `next_run_at` is picked up immediately."""
        self._wake.set()

    async def _run(self) -> None:
        while not self._stopping.is_set():
            if not await self._wait_unlocked():
                return
            now = utcnow()
            await self._tick(now)
            sleep_for = await self._sleep_duration()
            await self._wait_for(sleep_for)

    async def _wait_unlocked(self) -> bool:
        """Park while the vault is locked. False only if stopping while still locked."""
        return await wait_unlocked_or_stop(self._vault.unlocked_event, self._stopping)

    async def _wait_for(self, seconds: float | None) -> None:
        """Sleep until the earliest due task, a wake, or a stop. `None` means nothing is
        scheduled. A tiny `seconds` is floored to `_MIN_POLL_S` so a task whose advance
        failed is retried at a bounded rate, not in a busy loop."""
        if self._stopping.is_set():
            self._wake.clear()
            return
        stop_wait: asyncio.Task[bool] = asyncio.ensure_future(self._stopping.wait())
        wake_wait: asyncio.Task[bool] = asyncio.ensure_future(self._wake.wait())
        waits = {stop_wait, wake_wait}
        if seconds is not None:
            waits.add(asyncio.ensure_future(asyncio.sleep(max(seconds, _MIN_POLL_S), True)))
        try:
            _ = await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for fut in waits:
                _ = fut.cancel()
            _ = await asyncio.gather(*waits, return_exceptions=True)
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
        advance leaves the task due for the next tick rather than firing it unclaimed."""
        try:
            if task.schedule_type == ScheduleType.ONCE:
                await self.advance(task.id, next_run_at=None, enabled=False)
            else:
                next_run = compute_next_run(
                    task.schedule_type,
                    anchor=now,
                    run_at=task.run_at,
                    every_seconds=task.every_seconds,
                    cron_expr=task.cron_expr,
                    timezone=task.timezone,
                )
                await self.advance(task.id, next_run_at=next_run)
        except Exception:  # a failed reschedule must never kill the loop
            logger.exception("scheduler: task %s schedule advance failed", task.id)
            return
        _ = self._start(task, now)

    async def fire_now(self, task_id: str) -> str | None:
        """Fire ``task_id`` immediately without touching its schedule (manual ``run_now``
        and webhooks). Returns the `TaskRun` id, or ``None`` if the task does not exist, is
        disabled, or awaits approval: turning a task off stops its webhook too."""

        def work(session: Session) -> ScheduledTask | None:
            return session.get(ScheduledTask, task_id)

        task = await in_session(self._engine, work)
        if task is None or not task.enabled or not task.approved:
            return None
        return self._start(task, utcnow())

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
            view = replace(self._decrypt_view(task), task_run_id=run_row_id)
            if task.kind == TaskKind.REMINDER:
                await self._notify(view)
                result = TaskRunResult(outcome=TaskOutcome.OK.value)
            else:
                result = await self._executor(view)
        except TaskRunSuspended:
            return
        except Exception as exc:  # a bad task must never kill the loop
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
            # Stopped with the vault still locked: the row stays unfinalized.
            return
        # Bookkeeping only; a failure here must never kill the loop.
        try:
            await self.advance(task.id, last_run_at=finished)
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

    async def _load_due(self, now: datetime) -> list[ScheduledTask]:
        # The due check runs in Python, not SQL, because SQLite returns naive datetimes
        # that `as_utc` corrects.
        def work(session: Session) -> list[ScheduledTask]:
            return list(
                session.exec(
                    select(ScheduledTask)
                    .where(col(ScheduledTask.enabled).is_(True))
                    .where(col(ScheduledTask.approved).is_(True))
                    .where(col(ScheduledTask.next_run_at).is_not(None))
                ).all()
            )

        rows = await in_session(self._engine, work)
        # Nullable on the model; only the SQL filter excludes nulls, so re-check for typing.
        return [
            row for row in rows if row.next_run_at is not None and as_utc(row.next_run_at) <= now
        ]

    async def _earliest_next_run(self) -> datetime | None:
        def work(session: Session) -> list[datetime | None]:
            return list(
                session.exec(
                    select(ScheduledTask.next_run_at)
                    .where(col(ScheduledTask.enabled).is_(True))
                    .where(col(ScheduledTask.approved).is_(True))
                    .where(col(ScheduledTask.next_run_at).is_not(None))
                ).all()
            )

        rows = await in_session(self._engine, work)
        return min((as_utc(at) for at in rows if at is not None), default=None)

    async def _insert_task_run(self, *, id: str, task_id: str, started_at: datetime) -> None:
        def work(session: Session) -> None:
            session.add(TaskRun(id=id, task_id=task_id, started_at=started_at))

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
        (`encrypt_str` on the summary raises `VaultLocked` after the executor returned).
        Like `WriteBehindWorker`, a lock is a park, not a failed attempt; otherwise the
        exception would escape the bare task and strand the row and the reschedule. A
        transient locked database is bounded-retried (`_retry_locked`) rather than parked,
        since nothing signals when it clears. Returns False only if the loop is stopping
        while the vault never unlocked."""
        while True:
            try:
                await self._retry_locked(
                    lambda: self.finalize_task_run(
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

    async def finalize_task_run(
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
            if row is None or row.finished_at is not None:
                return
            row.finished_at = finished_at
            row.outcome = outcome
            row.run_id = run_id or row.run_id
            row.conversation_id = conversation_id or row.conversation_id
            row.summary_enc = summary_enc
            session.add(row)

        await in_session(self._engine, work)

    async def link_task_run(
        self, run_row_id: str, *, run_id: str | None, conversation_id: str | None
    ) -> None:
        """Point an open `TaskRun` at the run it drives, so a restored run finds its row;
        ``None`` takes the pointer back when that run was never started."""

        def work(session: Session) -> None:
            row = session.get(TaskRun, run_row_id)
            if row is None or row.finished_at is not None:
                return
            row.run_id = run_id
            row.conversation_id = conversation_id
            session.add(row)

        await self._retry_locked(
            lambda: in_session(self._engine, work), what=f"task run {run_row_id} link"
        )

    async def settle_restored(
        self, run_id: str, *, outcome: str, summary: str | None
    ) -> ScheduledTaskView | None:
        """Finalize the open `TaskRun` linked to ``run_id``, for a run that ended in a
        process other than the one that fired it. Returns the routine's view when a row
        was closed, ``None`` when none was open."""

        def find(session: Session) -> tuple[str, ScheduledTask] | None:
            row = session.exec(
                select(TaskRun)
                .where(TaskRun.run_id == run_id)
                .where(col(TaskRun.finished_at).is_(None))
            ).first()
            task = session.get(ScheduledTask, row.task_id) if row is not None else None
            return (row.id, task) if row is not None and task is not None else None

        found = await in_session(self._engine, find)
        if found is None:
            return None
        run_row_id, task = found
        finished = utcnow()
        _ = await self._finalize_parked(
            run_row_id,
            finished_at=finished,
            outcome=outcome,
            run_id=run_id,
            conversation_id=None,
            summary=summary,
        )
        try:
            await self.advance(task.id, last_run_at=finished)
        except Exception:
            logger.exception("scheduler: task %s last-run stamp failed", task.id)
        return self._decrypt_view(task)

    async def records_run(self, run_id: str) -> bool:
        """Whether a `TaskRun` is linked to ``run_id``, i.e. its routine still exists."""

        def work(session: Session) -> bool:
            return (
                session.exec(select(TaskRun.id).where(TaskRun.run_id == run_id)).first() is not None
            )

        return await in_session(self._engine, work)

    async def open_run_ids(self, task_id: str) -> list[str]:
        """The runs linked to ``task_id``'s unfinished `TaskRun` rows."""

        def work(session: Session) -> list[str]:
            rows = session.exec(
                select(TaskRun.run_id)
                .where(TaskRun.task_id == task_id)
                .where(col(TaskRun.finished_at).is_(None))
                .where(col(TaskRun.run_id).is_not(None))
            )
            return [run_id for run_id in rows if run_id is not None]

        return await in_session(self._engine, work)

    async def close_stranded(self, is_live: Callable[[str], bool]) -> int:
        """Finalize every open `TaskRun` that nothing is going to finish: not an execution
        of this process, and not linked to a run for which ``is_live`` holds. Returns how
        many were closed."""

        def open_rows(session: Session) -> list[tuple[str, str | None]]:
            rows = session.exec(select(TaskRun).where(col(TaskRun.finished_at).is_(None)))
            return [(row.id, row.run_id) for row in rows]

        closed = 0
        for run_row_id, run_id in await in_session(self._engine, open_rows):
            if run_row_id in self._running or (run_id is not None and is_live(run_id)):
                continue
            _ = await self._finalize_parked(
                run_row_id,
                finished_at=utcnow(),
                outcome=TaskOutcome.ERROR.value,
                run_id=run_id,
                conversation_id=None,
                summary="the app stopped before this run finished",
            )
            closed += 1
        return closed

    async def _retry_locked(self, attempt: Callable[[], Awaitable[None]], *, what: str) -> None:
        """Run a DB write, riding out SQLite's transient `database is locked`/`busy` (often a
        write-behind drainer mid-flush). Bounded with backoff so a permanent error still
        surfaces."""
        delay = _DB_LOCK_RETRY_BASE_S
        for attempts_left in range(DB_LOCK_RETRIES - 1, -1, -1):
            try:
                return await attempt()
            except OperationalError as exc:
                if not _is_transient_lock(exc):
                    raise
                if attempts_left == 0:
                    logger.error(
                        "scheduler: %s still hitting a locked database after %d attempts"
                        + " — giving up",
                        what,
                        DB_LOCK_RETRIES,
                    )
                    raise
                logger.warning(
                    "scheduler: %s hit a locked database — retrying in %.2fs", what, delay
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, _DB_LOCK_RETRY_MAX_S)

    async def advance(
        self,
        task_id: str,
        *,
        next_run_at: datetime | None | _Keep = _KEEP,
        last_run_at: datetime | None = None,
        enabled: bool | None = None,
    ) -> None:
        """Update a task's schedule row, riding out a transient locked database; moving
        `next_run_at` claims an occurrence, so brief contention must not delay the fire."""
        await self._retry_locked(
            lambda: self.advance_once(
                task_id, next_run_at=next_run_at, last_run_at=last_run_at, enabled=enabled
            ),
            what=f"task {task_id} schedule advance",
        )

    async def advance_once(
        self,
        task_id: str,
        *,
        next_run_at: datetime | None | _Keep = _KEEP,
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
