"""SchedulerService: the tick loop, non-overlap, lock-awareness, and its two
injected executor/notify paths. See test_task_schedule.py for the pure next-run
math this loop leans on."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import timedelta
from pathlib import Path

from sqlalchemy.exc import OperationalError
from sqlmodel import Session, select

import services.scheduler as scheduler_mod
from core.db import init_db, make_engine, read_session
from core.serde import as_utc
from core.vault import Vault
from models._fields import utcnow
from models.task import ScheduledTask, ScheduleType, TaskKind, TaskOutcome, TaskOutput, TaskRun
from services.scheduler import ScheduledTaskView, SchedulerService, TaskRunResult

OWNER = "operator"


async def _vault(tmp_path: Path) -> Vault:
    v = Vault(tmp_path / "keyfile.json")
    await v.setup("pw")
    return v


def _engine():
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    return engine


def _add_task(
    engine,
    vault: Vault,
    *,
    kind: str = TaskKind.AGENT.value,
    schedule_type: str = ScheduleType.ONCE.value,
    run_at=None,
    every_seconds: float | None = None,
    cron_expr: str | None = None,
    output: str = TaskOutput.CHAT.value,
    next_run_at=None,
    enabled: bool = True,
    title: str = "a task",
    prompt: str = "do the thing",
) -> str:
    task = ScheduledTask(
        owner_id=OWNER,
        kind=kind,
        title_enc=vault.encrypt_str(title),
        prompt_enc=vault.encrypt_str(prompt),
        schedule_type=schedule_type,
        run_at=run_at,
        every_seconds=every_seconds,
        cron_expr=cron_expr,
        output=output,
        pre_authorized=[],
        enabled=enabled,
        next_run_at=next_run_at,
    )
    with Session(engine) as session:
        session.add(task)
        session.commit()
        session.refresh(task)
        return task.id


# Both readers below are called *while* the scheduler's tick loop is still driving the
# same in-memory engine, which is a single shared connection — so they take the engine's
# connection lock like `in_session` does. A bare `Session(engine)` here raced the loop
# into "cannot start a transaction within a transaction".
def _get_task(engine, task_id: str) -> ScheduledTask:
    with read_session(engine) as session:
        return session.get(ScheduledTask, task_id)


def _list_runs(engine, task_id: str) -> list[TaskRun]:
    with read_session(engine) as session:
        return list(session.exec(select(TaskRun).where(TaskRun.task_id == task_id)).all())


async def _settled_runs(
    engine, task_id: str, *, count: int = 1, timeout: float = 5.0
) -> list[TaskRun]:
    """``task_id``'s runs, once at least ``count`` exist and every one has finished.

    **Wait for the condition, not for a plausible number of milliseconds.** `fire_now`
    returns as soon as the `TaskRun` row exists; the executor and the bookkeeping that
    finalizes `outcome` run on the scheduler's own loop afterwards. A fixed
    `asyncio.sleep(0.2)` is therefore a bet on scheduling latency — one that holds on an
    idle machine and loses when the whole suite is running in parallel, which is the only
    time it ever failed.

    ``outcome is None`` is what makes this a real wait rather than a faster guess: the row
    is inserted with it unset and only filled in once the executor has returned, so a run
    that satisfies this predicate has already appended to whatever the test is asserting
    about. The deadline is generous on purpose — reaching it means something is genuinely
    stuck, and a timeout that could be hit by a busy machine would be the same bug again.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        runs = _list_runs(engine, task_id)
        if len(runs) >= count and all(run.outcome is not None for run in runs):
            return runs
        if loop.time() >= deadline:
            raise AssertionError(
                f"no run settled for {task_id} within {timeout}s — saw {len(runs)} run(s): "
                f"{[(run.id, run.outcome) for run in runs]}"
            )
        await asyncio.sleep(0.01)


async def _never_notify(view: ScheduledTaskView) -> None:
    raise AssertionError("notify must not be called for an agent task")


async def _never_execute(view: ScheduledTaskView) -> TaskRunResult:
    raise AssertionError("executor must not be called for a reminder task")


# --- firing due tasks ---------------------------------------------------------


async def test_once_agent_task_fires_records_the_run_and_self_disables(tmp_path):
    engine = _engine()
    vault = await _vault(tmp_path)

    async def executor(view: ScheduledTaskView) -> TaskRunResult:
        assert view.output == TaskOutput.NOTIFICATION.value
        assert view.prompt == "do the thing"
        return TaskRunResult(
            outcome=TaskOutcome.OK.value,
            run_id="run-1",
            conversation_id="conv-1",
            summary="did it",
        )

    task_id = _add_task(
        engine,
        vault,
        schedule_type=ScheduleType.ONCE.value,
        run_at=utcnow(),
        next_run_at=utcnow(),
        output=TaskOutput.NOTIFICATION.value,
    )
    scheduler = SchedulerService(engine, vault, executor, _never_notify)
    await scheduler.start()
    try:
        await asyncio.sleep(0.2)
        runs = _list_runs(engine, task_id)
        assert len(runs) == 1
        assert runs[0].outcome == TaskOutcome.OK.value
        assert runs[0].run_id == "run-1"
        assert runs[0].conversation_id == "conv-1"
        assert vault.decrypt_str(runs[0].summary_enc) == "did it"

        task = _get_task(engine, task_id)
        assert task.enabled is False  # a spent "once" self-disables
        assert task.next_run_at is None
        assert task.last_run_at is not None
    finally:
        await scheduler.stop()


async def test_interval_task_recomputes_forward_and_stays_enabled(tmp_path):
    engine = _engine()
    vault = await _vault(tmp_path)
    calls: list[str] = []

    async def executor(view: ScheduledTaskView) -> TaskRunResult:
        calls.append(view.id)
        return TaskRunResult(outcome=TaskOutcome.OK.value)

    before = utcnow()
    task_id = _add_task(
        engine,
        vault,
        schedule_type=ScheduleType.INTERVAL.value,
        every_seconds=3600,
        next_run_at=before,
    )
    scheduler = SchedulerService(engine, vault, executor, _never_notify)
    await scheduler.start()
    try:
        await asyncio.sleep(0.2)
        assert calls == [task_id]
        task = _get_task(engine, task_id)
        assert task.enabled is True
        # Anchored on settlement (~now), not left at the stale past next_run_at.
        assert as_utc(task.next_run_at) > before + timedelta(seconds=3500)
    finally:
        await scheduler.stop()


async def test_reminder_task_calls_notify_verbatim_not_the_executor(tmp_path):
    engine = _engine()
    vault = await _vault(tmp_path)
    notified: list[tuple[str, str]] = []

    async def notify(view: ScheduledTaskView) -> None:
        notified.append((view.title, view.prompt))

    task_id = _add_task(
        engine,
        vault,
        kind=TaskKind.REMINDER.value,
        schedule_type=ScheduleType.ONCE.value,
        run_at=utcnow(),
        next_run_at=utcnow(),
        title="Take a break",
        prompt="Stand up and stretch",
    )
    scheduler = SchedulerService(engine, vault, _never_execute, notify)
    await scheduler.start()
    try:
        await asyncio.sleep(0.2)
        assert notified == [("Take a break", "Stand up and stretch")]
        runs = _list_runs(engine, task_id)
        assert len(runs) == 1
        assert runs[0].outcome == TaskOutcome.OK.value
    finally:
        await scheduler.stop()


async def test_executor_exception_records_error_outcome_without_killing_the_loop(tmp_path):
    engine = _engine()
    vault = await _vault(tmp_path)

    async def bad_executor(view: ScheduledTaskView) -> TaskRunResult:
        raise RuntimeError("boom")

    task_id = _add_task(
        engine, vault, schedule_type=ScheduleType.ONCE.value, run_at=utcnow(), next_run_at=utcnow()
    )
    scheduler = SchedulerService(engine, vault, bad_executor, _never_notify)
    await scheduler.start()
    try:
        await asyncio.sleep(0.2)
        runs = _list_runs(engine, task_id)
        assert len(runs) == 1
        assert runs[0].outcome == TaskOutcome.ERROR.value
        assert vault.decrypt_str(runs[0].summary_enc) == "boom"
        # A raising executor still counts as "it ran" — a once still self-disables.
        assert _get_task(engine, task_id).enabled is False
    finally:
        await scheduler.stop()


async def test_advance_failure_releases_the_task_so_the_next_due_fire_runs(tmp_path):
    """A DB error in the post-finalize schedule advance must not strand the task id
    in `_running` — a phantom in-flight entry would queue every future due fire
    behind a run that never ends. The failed advance leaves `next_run_at` at its
    stale past value, so the very next tick re-detects the task as due and fires it."""
    engine = _engine()
    vault = await _vault(tmp_path)
    second_fire = asyncio.Event()
    calls: list[str] = []

    async def executor(view: ScheduledTaskView) -> TaskRunResult:
        calls.append(view.id)
        if len(calls) >= 2:
            second_fire.set()
        return TaskRunResult(outcome=TaskOutcome.OK.value)

    task_id = _add_task(
        engine,
        vault,
        schedule_type=ScheduleType.INTERVAL.value,
        every_seconds=3600,
        next_run_at=utcnow(),
    )
    scheduler = SchedulerService(engine, vault, executor, _never_notify)
    real_advance = scheduler._advance
    failed = False

    async def flaky_advance(task_id: str, **kwargs) -> None:
        # Only the fire path passes `last_run_at`.
        nonlocal failed
        if not failed and kwargs.get("last_run_at") is not None:
            failed = True
            raise RuntimeError("transient DB failure")
        await real_advance(task_id, **kwargs)

    scheduler._advance = flaky_advance
    await scheduler.start()
    try:
        await asyncio.wait_for(second_fire.wait(), timeout=2.0)
        await asyncio.sleep(0.2)  # let the second fire's own bookkeeping settle
        live = scheduler._running.get(task_id)
        assert live is None or live.done()  # no phantom in-flight entry
        completed = [r for r in _list_runs(engine, task_id) if r.outcome == TaskOutcome.OK.value]
        assert len(completed) == 2  # the re-detected fire ran
        assert all(r.finished_at is not None for r in completed)  # both rows finalized
    finally:
        await scheduler.stop()


async def test_compute_next_run_failure_still_finalizes_and_loop_survives(tmp_path, monkeypatch):
    """An unexpected error in `compute_next_run` after finalize must be contained the
    same way: the `TaskRun` row stays finalized, the task id leaves `_running`, and a
    subsequent due detection fires the task again instead of skipping forever."""
    engine = _engine()
    vault = await _vault(tmp_path)
    fired_again = asyncio.Event()
    calls: list[str] = []

    async def executor(view: ScheduledTaskView) -> TaskRunResult:
        calls.append(view.id)
        if len(calls) >= 2:
            fired_again.set()
        return TaskRunResult(outcome=TaskOutcome.OK.value)

    task_id = _add_task(
        engine,
        vault,
        schedule_type=ScheduleType.INTERVAL.value,
        every_seconds=3600,
        next_run_at=utcnow(),
    )
    scheduler = SchedulerService(engine, vault, executor, _never_notify)

    real_compute = scheduler_mod.compute_next_run
    failed = False

    def flaky_compute(schedule_type: str, **kwargs):
        nonlocal failed
        if not failed:
            failed = True
            raise RuntimeError("recompute blew up")
        return real_compute(schedule_type, **kwargs)

    monkeypatch.setattr(scheduler_mod, "compute_next_run", flaky_compute)

    # Fire on demand (no tick loop yet) so the failing recompute deterministically
    # hits this fire's own path, never a concurrent skip's.
    run_row_id = await scheduler.fire_now(task_id)
    assert run_row_id is not None
    fire_task = scheduler._running.get(task_id)
    if fire_task is not None:
        await asyncio.wait_for(fire_task, timeout=2.0)
    assert task_id not in scheduler._running  # released despite the raise
    runs = {r.id: r for r in _list_runs(engine, task_id)}
    assert runs[run_row_id].outcome == TaskOutcome.OK.value
    assert runs[run_row_id].finished_at is not None  # still finalized

    # The advance never landed, so the task is still due — a live loop must pick it
    # up and run it for real (the loop survived; no permanent phantom skip).
    await scheduler.start()
    try:
        await asyncio.wait_for(fired_again.wait(), timeout=2.0)
        assert calls == [task_id, task_id]
    finally:
        await scheduler.stop()


# --- transient locked database ---------------------------------------------------


def _locked_error() -> OperationalError:
    """What SQLAlchemy raises when SQLite's write lock is held by another connection
    (e.g. a write-behind drainer flushing concurrently) past the busy_timeout."""
    return OperationalError("UPDATE taskrun …", {}, sqlite3.OperationalError("database is locked"))


async def test_transient_locked_db_on_finalize_retries_and_never_double_fires(tmp_path):
    """The live-drive regression: a concurrent write-behind drainer held SQLite's
    write lock, `_finalize_task_run` raised `database is locked`, the error escaped
    `_finalize_parked` (which only parked on `VaultLocked`) so `_advance` never ran —
    yet `_fire`'s `finally` still freed the in-flight claim, and the next tick
    re-dispatched a duplicate REAL fire. A transient lock must be retried like a
    vault park: exactly one fire, the row finalized, a spent `once` disabled."""
    engine = _engine()
    vault = await _vault(tmp_path)
    calls: list[str] = []

    async def executor(view: ScheduledTaskView) -> TaskRunResult:
        calls.append(view.id)
        return TaskRunResult(outcome=TaskOutcome.OK.value, summary="ran once")

    task_id = _add_task(
        engine, vault, schedule_type=ScheduleType.ONCE.value, run_at=utcnow(), next_run_at=utcnow()
    )
    scheduler = SchedulerService(engine, vault, executor, _never_notify)
    real_finalize = scheduler._finalize_task_run
    failed = False

    async def flaky_finalize(run_row_id: str, **kwargs) -> None:
        nonlocal failed
        if not failed:
            failed = True
            raise _locked_error()
        await real_finalize(run_row_id, **kwargs)

    scheduler._finalize_task_run = flaky_finalize
    await scheduler.start()
    try:
        # Long enough to span the retry backoff plus several ticks — with the old
        # behavior the still-due task re-fires well within this window.
        await asyncio.sleep(0.5)
        assert calls == [task_id]  # exactly one REAL fire, no phantom re-dispatch
        runs = _list_runs(engine, task_id)
        assert len(runs) == 1  # no orphaned second TaskRun row either
        assert runs[0].outcome == TaskOutcome.OK.value
        assert runs[0].finished_at is not None  # the retried finalize landed
        task = _get_task(engine, task_id)
        assert task.enabled is False  # the once self-disabled — not re-fireable
        assert task.next_run_at is None
        live = scheduler._running.get(task_id)
        assert live is None or live.done()  # the claim isn't stranded
    finally:
        await scheduler.stop()


async def test_transient_locked_db_on_advance_retries_and_schedule_still_advances(tmp_path):
    """Same invariant one write later: finalize lands, then the schedule advance hits
    a transient lock. `_advance` must retry it out — a once task that stayed due with
    its claim freed would be re-dispatched for real on the very next tick."""
    engine = _engine()
    vault = await _vault(tmp_path)
    calls: list[str] = []

    async def executor(view: ScheduledTaskView) -> TaskRunResult:
        calls.append(view.id)
        return TaskRunResult(outcome=TaskOutcome.OK.value)

    task_id = _add_task(
        engine, vault, schedule_type=ScheduleType.ONCE.value, run_at=utcnow(), next_run_at=utcnow()
    )
    scheduler = SchedulerService(engine, vault, executor, _never_notify)
    real_advance_once = scheduler._advance_once
    failed = False

    async def flaky_advance_once(task_id: str, **kwargs) -> None:
        nonlocal failed
        if not failed:
            failed = True
            raise _locked_error()
        await real_advance_once(task_id, **kwargs)

    scheduler._advance_once = flaky_advance_once
    await scheduler.start()
    try:
        await asyncio.sleep(0.5)
        assert calls == [task_id]  # one fire — the retried advance closed the window
        runs = _list_runs(engine, task_id)
        assert len(runs) == 1
        assert runs[0].outcome == TaskOutcome.OK.value
        assert runs[0].finished_at is not None
        task = _get_task(engine, task_id)
        assert task.enabled is False
        assert task.next_run_at is None
    finally:
        await scheduler.stop()


async def test_permanently_locked_db_gives_up_after_bounded_retries(tmp_path, monkeypatch):
    """A lock that never clears must surface after the bounded retry budget — never
    loop forever holding the fire task open (a permanent DB failure has to become
    visible, not spin silently)."""
    monkeypatch.setattr(scheduler_mod, "_DB_LOCK_RETRY_BASE_S", 0.01)
    monkeypatch.setattr(scheduler_mod, "_DB_LOCK_RETRY_MAX_S", 0.02)
    engine = _engine()
    vault = await _vault(tmp_path)

    async def executor(view: ScheduledTaskView) -> TaskRunResult:
        return TaskRunResult(outcome=TaskOutcome.OK.value)

    task_id = _add_task(
        engine, vault, schedule_type=ScheduleType.ONCE.value, run_at=utcnow(), next_run_at=utcnow()
    )
    scheduler = SchedulerService(engine, vault, executor, _never_notify)
    attempts = 0

    async def always_locked(run_row_id: str, **kwargs) -> None:
        nonlocal attempts
        attempts += 1
        raise _locked_error()

    scheduler._finalize_task_run = always_locked

    # Fire on demand (no tick loop) so the one detached fire task is directly awaitable.
    run_row_id = await scheduler.fire_now(task_id)
    assert run_row_id is not None
    fire_task = scheduler._running.get(task_id)
    assert fire_task is not None
    [outcome] = await asyncio.wait_for(
        asyncio.gather(fire_task, return_exceptions=True), timeout=2.0
    )
    assert isinstance(outcome, OperationalError)  # surfaced, not swallowed or looped
    assert attempts == scheduler_mod._DB_LOCK_RETRIES  # exactly the bounded budget
    assert task_id not in scheduler._running  # the claim was still released


# --- non-overlap ---------------------------------------------------------------


def _slow():
    executing = asyncio.Event()
    release = asyncio.Event()
    calls: list[str] = []

    async def executor(view: ScheduledTaskView) -> TaskRunResult:
        calls.append(view.id)
        executing.set()
        await release.wait()
        return TaskRunResult(outcome=TaskOutcome.OK.value)

    return executor, executing, release, calls


async def test_a_fire_due_while_the_last_is_live_runs_after_it(tmp_path):
    engine = _engine()
    vault = await _vault(tmp_path)
    executor, executing, release, calls = _slow()
    task_id = _add_task(
        engine,
        vault,
        schedule_type=ScheduleType.INTERVAL.value,
        every_seconds=3600,
        next_run_at=utcnow() - timedelta(milliseconds=10),
    )
    scheduler = SchedulerService(engine, vault, executor, _never_notify)
    await scheduler.start()
    try:
        await asyncio.wait_for(executing.wait(), timeout=2.0)
        # The next occurrence comes due while the first is still running.
        await scheduler._advance(task_id, next_run_at=utcnow() - timedelta(milliseconds=10))
        scheduler.wake()
        await asyncio.sleep(0.2)
        assert len(calls) == 1  # never two at once
        assert task_id in scheduler._queued

        release.set()
        runs = await _settled_runs(engine, task_id, count=2)
        assert [r.outcome for r in runs] == ["ok", "ok"]
        assert len(calls) == 2
    finally:
        await scheduler.stop()


async def test_a_queued_fire_stays_due_across_a_restart(tmp_path):
    engine = _engine()
    vault = await _vault(tmp_path)
    executor, executing, release, _calls = _slow()
    task_id = _add_task(
        engine,
        vault,
        schedule_type=ScheduleType.INTERVAL.value,
        every_seconds=3600,
        next_run_at=utcnow() - timedelta(milliseconds=10),
    )
    scheduler = SchedulerService(engine, vault, executor, _never_notify)
    await scheduler.start()
    await asyncio.wait_for(executing.wait(), timeout=2.0)
    overdue = utcnow() - timedelta(milliseconds=10)
    await scheduler._advance(task_id, next_run_at=overdue)
    scheduler.wake()
    await asyncio.sleep(0.2)
    assert task_id in scheduler._queued
    assert as_utc(_get_task(engine, task_id).next_run_at) == overdue
    release.set()
    await scheduler.stop()


# --- lock-awareness --------------------------------------------------------------


async def test_vault_locked_mid_execution_parks_finalize_until_unlock(tmp_path):
    """The operator can lock the vault via `POST /auth/lock` while an agent task's
    executor is still running. The finalize step (which encrypts the summary) must
    park and retry rather than let `VaultLocked` escape the detached `_fire` task —
    otherwise the `TaskRun` row is stranded unfinished and the task's own
    reschedule never advances."""
    engine = _engine()
    vault = await _vault(tmp_path)

    async def executor(view: ScheduledTaskView) -> TaskRunResult:
        vault.lock()  # simulates the operator locking mid-execution
        return TaskRunResult(outcome=TaskOutcome.OK.value, summary="finished while locked")

    task_id = _add_task(
        engine, vault, schedule_type=ScheduleType.ONCE.value, run_at=utcnow(), next_run_at=utcnow()
    )
    scheduler = SchedulerService(engine, vault, executor, _never_notify)
    await scheduler.start()
    try:
        await asyncio.sleep(0.2)
        # Finalize is parked, not dropped: the row exists but isn't finished yet,
        # and the task's own schedule hasn't advanced.
        runs = _list_runs(engine, task_id)
        assert len(runs) == 1
        assert runs[0].finished_at is None
        assert _get_task(engine, task_id).enabled is True

        await vault.unlock("pw")
        await asyncio.sleep(0.2)

        # The tick loop may also wake right as the vault unlocks and re-detect the
        # still-due task while the original fire is finishing its parked finalize —
        # a legitimate non-overlap "skipped" row, the same race
        # `test_still_live_execution_is_skipped_not_run_twice` already exercises.
        # What matters here is that the original fire's own row finalized exactly
        # once, with its summary intact, rather than being dropped.
        runs = _list_runs(engine, task_id)
        completed = [r for r in runs if r.outcome == TaskOutcome.OK.value]
        assert len(completed) == 1
        assert completed[0].finished_at is not None
        assert vault.decrypt_str(completed[0].summary_enc) == "finished while locked"
        assert _get_task(engine, task_id).enabled is False  # once task self-disables
    finally:
        await scheduler.stop()


async def test_locked_vault_parks_and_fires_at_most_one_catch_up_on_unlock(tmp_path):
    engine = _engine()
    vault = await _vault(tmp_path)
    calls: list[str] = []

    async def executor(view: ScheduledTaskView) -> TaskRunResult:
        calls.append(view.id)
        return TaskRunResult(outcome=TaskOutcome.OK.value)

    overdue = utcnow() - timedelta(hours=5)  # long overdue: a locked vault or a long boot gap
    task_id = _add_task(
        engine,
        vault,
        schedule_type=ScheduleType.INTERVAL.value,
        every_seconds=60,
        next_run_at=overdue,
    )
    vault.lock()
    scheduler = SchedulerService(engine, vault, executor, _never_notify)
    await scheduler.start()
    try:
        await asyncio.sleep(0.1)
        assert calls == []  # parked while locked — never touches the encrypted prompt

        await vault.unlock("pw")
        await asyncio.sleep(0.2)
        assert calls == [task_id]  # exactly one catch-up fire, not a backlog burst

        task = _get_task(engine, task_id)
        # Recomputed from now, not the stale hour-old timestamp.
        assert as_utc(task.next_run_at) > utcnow()
    finally:
        await scheduler.stop()


# --- wake / re-arm ---------------------------------------------------------------


async def test_wake_picks_up_a_freshly_armed_task_without_waiting_out_the_old_sleep(tmp_path):
    engine = _engine()
    vault = await _vault(tmp_path)
    fired = asyncio.Event()

    async def executor(view: ScheduledTaskView) -> TaskRunResult:
        fired.set()
        return TaskRunResult(outcome=TaskOutcome.OK.value)

    scheduler = SchedulerService(engine, vault, executor, _never_notify)
    await scheduler.start()
    try:
        await asyncio.sleep(0.05)  # nothing scheduled yet — the loop parks indefinitely

        # What a task-creation route does: insert directly, then nudge the scheduler.
        _add_task(
            engine,
            vault,
            schedule_type=ScheduleType.ONCE.value,
            run_at=utcnow(),
            next_run_at=utcnow(),
        )
        scheduler.wake()

        await asyncio.wait_for(fired.wait(), timeout=1.0)
    finally:
        await scheduler.stop()


# --- on-demand fire (fire_now) ---------------------------------------------------


async def test_fire_now_fires_a_not_yet_due_task_immediately(tmp_path):
    engine = _engine()
    vault = await _vault(tmp_path)
    calls: list[str] = []

    async def executor(view: ScheduledTaskView) -> TaskRunResult:
        calls.append(view.id)
        return TaskRunResult(outcome=TaskOutcome.OK.value, summary="ran on demand")

    task_id = _add_task(
        engine,
        vault,
        schedule_type=ScheduleType.ONCE.value,
        run_at=utcnow() + timedelta(hours=1),
        next_run_at=utcnow() + timedelta(hours=1),  # not due for an hour
    )
    scheduler = SchedulerService(engine, vault, executor, _never_notify)
    await scheduler.start()
    try:
        task_run_id = await scheduler.fire_now(task_id)
        assert task_run_id is not None
        runs = await _settled_runs(engine, task_id)
        assert calls == [task_id]
        assert len(runs) == 1
        assert runs[0].id == task_run_id
        assert runs[0].outcome == TaskOutcome.OK.value
    finally:
        await scheduler.stop()


async def test_fire_now_returns_none_for_an_unknown_task(tmp_path):
    engine = _engine()
    vault = await _vault(tmp_path)
    scheduler = SchedulerService(engine, vault, _never_execute, _never_notify)
    await scheduler.start()
    try:
        assert await scheduler.fire_now("nope") is None
    finally:
        await scheduler.stop()


async def test_fire_now_refuses_a_disabled_task(tmp_path):
    # Mirrors the tick loop's own due-detection (`_load_due` filters on `enabled`) —
    # an operator disabling a task must actually stop it from firing on-demand too,
    # not just stop its own schedule. This is the one gate shared by `run_now` and
    # the auth-exempt inbound webhook (security-02/04): a leaked webhook token, or an
    # operator's own accidental click, must not still run the task once disabled.
    engine = _engine()
    vault = await _vault(tmp_path)
    task_id = _add_task(
        engine,
        vault,
        schedule_type=ScheduleType.ONCE.value,
        run_at=utcnow(),
        next_run_at=utcnow(),
        enabled=False,
    )
    scheduler = SchedulerService(engine, vault, _never_execute, _never_notify)
    await scheduler.start()
    try:
        assert await scheduler.fire_now(task_id) is None
        assert _list_runs(engine, task_id) == []  # never dispatched, no TaskRun at all
    finally:
        await scheduler.stop()


async def test_fire_now_shares_non_overlap_with_the_tick_loop(tmp_path):
    """A manual `fire_now` and the tick loop's own due-detection never run the same task
    twice at once — both go through `_dispatch` — and the manual fire is queued, not
    dropped."""
    engine = _engine()
    vault = await _vault(tmp_path)
    executor, executing, release, calls = _slow()
    task_id = _add_task(
        engine,
        vault,
        schedule_type=ScheduleType.INTERVAL.value,
        every_seconds=3600,
        next_run_at=utcnow() - timedelta(milliseconds=10),
    )
    scheduler = SchedulerService(engine, vault, executor, _never_notify)
    await scheduler.start()
    try:
        await asyncio.wait_for(executing.wait(), timeout=2.0)
        queued = await scheduler.fire_now(task_id)
        assert queued == await scheduler.fire_now(task_id)  # one waits, however many ask
        assert len(calls) == 1

        release.set()
        runs = await _settled_runs(engine, task_id, count=2)
        assert queued in {r.id for r in runs}
        assert len(calls) == 2
    finally:
        await scheduler.stop()


async def test_running_a_one_off_by_hand_keeps_its_scheduled_moment(tmp_path):
    engine = _engine()
    vault = await _vault(tmp_path)

    async def executor(view: ScheduledTaskView) -> TaskRunResult:
        return TaskRunResult(outcome=TaskOutcome.OK.value)

    later = utcnow() + timedelta(days=7)
    task_id = _add_task(
        engine, vault, schedule_type=ScheduleType.ONCE.value, run_at=later, next_run_at=later
    )
    scheduler = SchedulerService(engine, vault, executor, _never_notify)
    await scheduler.start()
    try:
        await scheduler.fire_now(task_id)
        await _settled_runs(engine, task_id)
        task = _get_task(engine, task_id)
        assert task.enabled and as_utc(task.next_run_at) == later
        assert task.last_run_at is not None
    finally:
        await scheduler.stop()
