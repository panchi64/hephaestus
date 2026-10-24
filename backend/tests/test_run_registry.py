"""RunRegistry: lifecycle, terminal mapping, bounds, cancellation, queueing."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import override

import pytest

from runs import (
    ConversationBusyError,
    LaneLimits,
    Orchestrator,
    ParkedJournal,
    Run,
    RunRegistry,
    RunStatus,
)
from runs.events import AnswerDelta, RunEnded, RunMetrics


def _types(run: Run) -> list[str]:
    return [e.body.type for e in run.stream.replay()]


def _ended(run: Run) -> RunEnded:
    body = run.stream.replay()[-1].body
    assert isinstance(body, RunEnded)
    return body


def _count_subscribers(seen: dict[str, int]) -> Callable[[Run], None]:
    def hook(run: Run) -> None:
        _ = seen.setdefault("count", run.stream.subscriber_count)

    return hook


async def test_run_completes_done():
    reg = RunRegistry()

    async def orch(run: Run) -> None:
        _ = run.emit(AnswerDelta(text="hi"))

    run = reg.submit(kind="chat", owner_id="operator", orchestrator=orch)
    await run.wait()

    assert run.status is RunStatus.done
    # No metrics frame: an empty one would blank the thread's meter.
    assert _types(run) == ["run.started", "answer.delta", "run.ended"]
    assert _ended(run).outcome == "done"


async def test_metrics_the_run_measured_precede_the_closing_frame_on_every_path():
    reg = RunRegistry()

    async def done(run: Run) -> None:
        run.set_metrics(RunMetrics(steps=2))

    async def failed(run: Run) -> None:
        run.set_metrics(RunMetrics(steps=2))
        raise ValueError("boom")

    async def stopped(run: Run) -> None:
        run.set_metrics(RunMetrics(steps=2))
        _ = await asyncio.Event().wait()

    for orch, closing in ((done, "run.ended"), (failed, "run.error"), (stopped, "run.ended")):
        run = reg.submit(kind="chat", owner_id="operator", orchestrator=orch)
        await asyncio.sleep(0)
        if orch is stopped:
            _ = await reg.cancel(run.id)
        await run.wait()
        types = _types(run)
        assert types[-2:] == ["run.metrics", closing]
        metrics = next(e.body for e in run.stream.replay() if e.body.type == "run.metrics")
        assert metrics.steps == 2


async def test_a_cancelled_or_errored_run_with_no_metrics_emits_no_metrics_frame():
    reg = RunRegistry()

    async def failed(_run: Run) -> None:
        raise ValueError("boom")

    async def stopped(_run: Run) -> None:
        _ = await asyncio.Event().wait()

    errored = reg.submit(kind="chat", owner_id="operator", orchestrator=failed)
    cancelled = reg.submit(kind="chat", owner_id="operator", orchestrator=stopped)
    await asyncio.sleep(0)
    _ = await reg.cancel(cancelled.id)
    await errored.wait()
    await cancelled.wait()

    assert "run.metrics" not in _types(errored)
    assert "run.metrics" not in _types(cancelled)
    assert _types(errored)[-1] == "run.error"
    assert _types(cancelled)[-1] == "run.ended"


async def test_run_error_is_terminal_not_fatal():
    reg = RunRegistry()

    async def orch(_run: Run) -> None:
        raise ValueError("boom")

    run = reg.submit(kind="chat", owner_id="operator", orchestrator=orch)
    await run.wait()

    assert run.status is RunStatus.error
    assert run.error == "boom"
    err = run.stream.replay()[-1].body
    assert err.type == "run.error"
    assert err.kind == "ValueError"
    assert err.message == "boom"


async def test_run_blocked_outcome():
    reg = RunRegistry()

    async def orch(run: Run) -> None:
        run.block("need more info")

    run = reg.submit(kind="agent", owner_id="operator", orchestrator=orch)
    await run.wait()

    assert run.status is RunStatus.blocked
    ended = run.stream.replay()[-1].body
    assert ended.type == "run.ended"
    assert ended.outcome == "blocked"
    assert ended.detail == "need more info"


async def test_cancel_running_run():
    reg = RunRegistry()

    async def orch(run: Run) -> None:
        _ = run.emit(AnswerDelta(text="working"))
        _ = await asyncio.Event().wait()

    run = reg.submit(kind="agent", owner_id="operator", orchestrator=orch)
    await asyncio.sleep(0)
    assert await reg.cancel(run.id) is True
    await run.wait()

    assert run.status is RunStatus.cancelled
    assert _ended(run).outcome == "cancelled"
    assert await reg.cancel(run.id) is False


async def test_wall_clock_timeout():
    reg = RunRegistry(wall_clock_timeout_s=0.05, inactivity_timeout_s=None)

    async def orch(run: Run) -> None:
        _ = run.emit(AnswerDelta(text="start"))
        await asyncio.sleep(5)

    run = reg.submit(kind="research", owner_id="operator", orchestrator=orch)
    await run.wait()

    assert run.status is RunStatus.blocked
    events = run.stream.replay()
    notice = next(e.body for e in events if e.body.type == "limit.notice")
    assert notice.limit == "time"
    assert events[-1].body.type == "run.ended"
    assert events[-1].body.outcome == "blocked"


async def test_inactivity_timeout():
    reg = RunRegistry(wall_clock_timeout_s=5.0, inactivity_timeout_s=0.05)

    async def orch(run: Run) -> None:
        _ = run.emit(AnswerDelta(text="start"))
        await asyncio.sleep(5)

    run = reg.submit(kind="research", owner_id="operator", orchestrator=orch)
    await run.wait()

    assert run.status is RunStatus.blocked
    events = run.stream.replay()
    notice = next(e.body for e in events if e.body.type == "limit.notice")
    assert notice.limit == "time"
    assert events[-1].body.type == "run.ended"
    assert events[-1].body.outcome == "blocked"


async def test_keepalive_holds_a_long_silent_call_open_past_the_inactivity_bound():
    # A non-streaming model call emits nothing between phase boundaries but is making progress.
    reg = RunRegistry(wall_clock_timeout_s=5.0, inactivity_timeout_s=0.1)

    async def orch(run: Run) -> None:
        _ = run.emit(AnswerDelta(text="start"))
        async with run.keepalive():
            await asyncio.sleep(0.5)

    run = reg.submit(kind="research", owner_id="operator", orchestrator=orch)
    await run.wait()

    assert run.status is RunStatus.done
    assert _ended(run).outcome == "done"


async def test_the_bound_applies_again_the_moment_the_call_returns():
    # The scope covers one awaited call; leaving it must re-arm the watchdog.
    reg = RunRegistry(wall_clock_timeout_s=5.0, inactivity_timeout_s=0.1)

    async def orch(run: Run) -> None:
        async with run.keepalive():
            await asyncio.sleep(0.3)
        await asyncio.sleep(5)

    run = reg.submit(kind="research", owner_id="operator", orchestrator=orch)
    await run.wait()

    assert run.status is RunStatus.blocked
    notice = next(e.body for e in run.stream.replay() if e.body.type == "limit.notice")
    assert notice.limit == "time"


async def test_keepalive_beats_inside_a_bound_shorter_than_its_default_interval():
    # The interval is a ceiling: a tight inactivity bound gets a proportionally faster heartbeat.
    reg = RunRegistry(wall_clock_timeout_s=5.0, inactivity_timeout_s=0.06)

    async def orch(run: Run) -> None:
        async with run.keepalive():
            await asyncio.sleep(0.4)

    run = reg.submit(kind="research", owner_id="operator", orchestrator=orch)
    await run.wait()

    assert run.status is RunStatus.done


async def test_keepalive_does_not_hide_a_failure_raised_inside_it():
    reg = RunRegistry(wall_clock_timeout_s=5.0, inactivity_timeout_s=1.0)

    async def orch(run: Run) -> None:
        async with run.keepalive():
            raise RuntimeError("the model refused")

    run = reg.submit(kind="research", owner_id="operator", orchestrator=orch)
    await run.wait()

    assert run.status is RunStatus.error
    assert "the model refused" in (run.error or "")


async def test_concurrency_limit_queues_bursts():
    # Same kind, so one lane: the second waits.
    reg = RunRegistry(lanes=LaneLimits(background=1))
    started1, release1 = asyncio.Event(), asyncio.Event()
    started2, release2 = asyncio.Event(), asyncio.Event()

    def orch(started: asyncio.Event, release: asyncio.Event) -> Orchestrator:
        async def _run(_run: Run) -> None:
            started.set()
            _ = await release.wait()

        return _run

    run1 = reg.submit(kind="t", owner_id="operator", orchestrator=orch(started1, release1))
    run2 = reg.submit(kind="t", owner_id="operator", orchestrator=orch(started2, release2))

    _ = await started1.wait()
    assert run1.status is RunStatus.running
    assert run2.status is RunStatus.queued

    release1.set()
    await run1.wait()
    _ = await started2.wait()
    assert run2.status is RunStatus.running

    release2.set()
    await run2.wait()
    assert run1.status is RunStatus.done
    assert run2.status is RunStatus.done


async def test_list_filters_by_owner():
    reg = RunRegistry()

    async def orch(_run: Run) -> None:
        return None

    a = reg.submit(kind="t", owner_id="alice", orchestrator=orch)
    b = reg.submit(kind="t", owner_id="bob", orchestrator=orch)
    _ = await asyncio.gather(a.wait(), b.wait())

    assert {r.id for r in reg.list(owner_id="alice")} == {a.id}
    assert {r.id for r in reg.list()} == {a.id, b.id}


async def test_active_run_for_finds_in_flight_run():
    reg = RunRegistry()
    started, release = asyncio.Event(), asyncio.Event()

    async def orch(_run: Run) -> None:
        started.set()
        _ = await release.wait()

    run = reg.submit(kind="chat", owner_id="operator", orchestrator=orch, conversation_id="c1")
    _ = await started.wait()

    assert reg.active_run_for("c1", "operator") is run
    assert reg.active_run_for("other", "operator") is None
    assert reg.active_run_for("c1", "intruder") is None

    release.set()
    await run.wait()

    assert reg.active_run_for("c1", "operator") is None


async def test_submit_rejects_a_second_run_for_the_same_conversation():
    # Backstop behind the route's 409 guard: no `await` between the check and registering
    # the run, which a route-level check can't guarantee.
    reg = RunRegistry()
    started, release = asyncio.Event(), asyncio.Event()

    async def orch(_run: Run) -> None:
        started.set()
        _ = await release.wait()

    run = reg.submit(kind="chat", owner_id="operator", orchestrator=orch, conversation_id="c1")
    _ = await started.wait()

    with pytest.raises(ConversationBusyError):
        _ = reg.submit(kind="chat", owner_id="operator", orchestrator=orch, conversation_id="c1")

    other_conv = reg.submit(
        kind="chat", owner_id="operator", orchestrator=orch, conversation_id="c2"
    )
    other_owner = reg.submit(
        kind="chat", owner_id="someone-else", orchestrator=orch, conversation_id="c1"
    )

    release.set()
    _ = await asyncio.gather(run.wait(), other_conv.wait(), other_owner.wait())

    run2 = reg.submit(kind="chat", owner_id="operator", orchestrator=orch, conversation_id="c1")
    await run2.wait()
    assert run2.status is RunStatus.done


async def test_active_run_for_prefers_the_most_recent():
    # `submit` refuses a second live run per conversation, so the two candidates are
    # registered directly to test the selection policy alone.
    from datetime import timedelta

    from runs.run import Run
    from runs.stream import RunStream

    reg = RunRegistry()
    older = Run(
        id="older", kind="chat", owner_id="operator", conversation_id="c1", stream=RunStream()
    )
    newer = Run(
        id="newer", kind="chat", owner_id="operator", conversation_id="c1", stream=RunStream()
    )
    newer.created_at = older.created_at + timedelta(seconds=1)
    reg.register(older)
    reg.register(newer)

    assert reg.active_run_for("c1", "operator") is newer

    newer.status = RunStatus.done
    assert reg.active_run_for("c1", "operator") is older


async def test_claim_rejects_a_second_claim_on_the_same_conversation():
    # A claim holds the conversation before any run is registered, which `active_run_for` can't see.
    reg = RunRegistry()
    reg.claim("c1", "operator")

    with pytest.raises(ConversationBusyError):
        reg.claim("c1", "operator")

    reg.claim("c2", "operator")
    reg.claim("c1", "someone-else")

    reg.release("c1", "operator")
    reg.release("c2", "operator")
    reg.release("c1", "someone-else")

    reg.claim("c1", "operator")
    reg.release("c1", "operator")


async def test_claim_rejects_when_a_run_is_already_live():
    reg = RunRegistry()
    started, release = asyncio.Event(), asyncio.Event()

    async def orch(_run: Run) -> None:
        started.set()
        _ = await release.wait()

    run = reg.submit(kind="chat", owner_id="operator", orchestrator=orch, conversation_id="c1")
    _ = await started.wait()

    with pytest.raises(ConversationBusyError):
        reg.claim("c1", "operator")

    release.set()
    await run.wait()

    reg.claim("c1", "operator")
    reg.release("c1", "operator")


def test_release_is_idempotent_without_a_prior_claim():
    reg = RunRegistry()
    reg.release("never-claimed", "operator")


async def test_on_terminal_fires_once_on_normal_completion():
    calls: list[Run] = []
    reg = RunRegistry(on_terminal=calls.append)

    async def orch(run: Run) -> None:
        _ = run.emit(AnswerDelta(text="hi"))

    run = reg.submit(kind="chat", owner_id="operator", orchestrator=orch)
    await run.wait()

    assert calls == [run]


async def test_on_terminal_fires_once_on_error():
    calls: list[Run] = []
    reg = RunRegistry(on_terminal=calls.append)

    async def orch(_run: Run) -> None:
        raise ValueError("boom")

    run = reg.submit(kind="chat", owner_id="operator", orchestrator=orch)
    await run.wait()

    assert calls == [run]


async def test_on_terminal_fires_once_on_blocked():
    calls: list[Run] = []
    reg = RunRegistry(on_terminal=calls.append)

    async def orch(run: Run) -> None:
        run.block("need more info")

    run = reg.submit(kind="agent", owner_id="operator", orchestrator=orch)
    await run.wait()

    assert calls == [run]


async def test_on_terminal_fires_once_on_cancel_while_running():
    calls: list[Run] = []
    reg = RunRegistry(on_terminal=calls.append)

    async def orch(_run: Run) -> None:
        _ = await asyncio.Event().wait()

    run = reg.submit(kind="agent", owner_id="operator", orchestrator=orch)
    await asyncio.sleep(0)
    assert await reg.cancel(run.id) is True
    await run.wait()

    assert calls == [run]


async def test_on_terminal_not_fired_on_park_but_fires_on_cancel_while_parked():
    # Cancelling a parked run is the one close() path that bypasses `_execute`.
    calls: list[Run] = []
    reg = RunRegistry(on_terminal=calls.append)

    async def orch(run: Run) -> None:
        run.park(None)

    run = reg.submit(kind="chat", owner_id="operator", orchestrator=orch)
    await run.wait()

    assert run.status is RunStatus.awaiting_input
    assert calls == []

    assert await reg.cancel(run.id) is True
    assert calls == [run]
    assert run.status is RunStatus.cancelled


async def test_on_terminal_hook_failure_is_swallowed_and_logged(
    caplog: pytest.LogCaptureFixture,
):
    def boom(_run: Run) -> None:
        raise RuntimeError("notifier exploded")

    reg = RunRegistry(on_terminal=boom)

    async def orch(run: Run) -> None:
        _ = run.emit(AnswerDelta(text="hi"))

    run = reg.submit(kind="chat", owner_id="operator", orchestrator=orch)
    await run.wait()

    assert run.status is RunStatus.done
    assert "on_terminal hook failed" in caplog.text


async def test_on_terminal_reads_the_live_subscriber_count_before_close():
    # The hook must see who was watching at the transition, before subscriber cleanup runs.
    seen: dict[str, int] = {}
    reg = RunRegistry(on_terminal=_count_subscribers(seen))

    ready, proceed = asyncio.Event(), asyncio.Event()

    async def orch(run: Run) -> None:
        _ = run.emit(AnswerDelta(text="hi"))
        ready.set()
        _ = await proceed.wait()

    run = reg.submit(kind="chat", owner_id="operator", orchestrator=orch)
    _ = await ready.wait()

    async def consume():
        async for _ in run.stream.subscribe():
            pass

    task = asyncio.create_task(consume())
    await asyncio.sleep(0)
    proceed.set()
    await run.wait()
    await task

    assert seen["count"] == 1


async def test_on_terminal_sees_zero_subscribers_when_nobody_is_watching():
    seen: dict[str, int] = {}
    reg = RunRegistry(on_terminal=_count_subscribers(seen))

    async def orch(run: Run) -> None:
        _ = run.emit(AnswerDelta(text="hi"))

    run = reg.submit(kind="chat", owner_id="operator", orchestrator=orch)
    await run.wait()

    assert seen["count"] == 0


async def test_cancel_while_parked_survives_an_orchestrator_still_unwinding():
    # A cancel while the orchestrator is still unwinding closes the stream; `_execute` must
    # not then emit onto it, which would flip `cancelled` to `error` and refire the hook.
    calls: list[Run] = []
    reg = RunRegistry(on_terminal=calls.append)
    parked = asyncio.Event()
    release = asyncio.Event()

    async def orch(run: Run) -> None:
        run.park(None)
        parked.set()
        _ = await release.wait()  # still running, but the run already reads as parked

    run = reg.submit(kind="chat", owner_id="operator", orchestrator=orch)
    _ = await parked.wait()

    assert await reg.cancel(run.id) is True
    release.set()
    assert run.task is not None
    _ = await asyncio.gather(run.task, return_exceptions=True)

    assert run.status is RunStatus.cancelled
    assert calls == [run]
    assert _types(run)[-1] == "run.ended"
    assert _ended(run).outcome == "cancelled"


async def test_a_bound_tripping_just_after_a_park_still_flushes_the_parked_turn():
    # `on_timeout` is disarmed at the park, so a bound tripping mid-unwind must still
    # persist the turn.
    flushed: list[str] = []
    reg = RunRegistry(inactivity_timeout_s=0.05)

    async def orch(run: Run) -> None:
        run.park(None)
        run.on_timeout = None
        run.on_park_cancel = lambda: flushed.append(run.id)
        _ = await asyncio.Event().wait()

    run = reg.submit(kind="chat", owner_id="operator", orchestrator=orch)
    await run.wait()

    assert run.status is RunStatus.blocked
    assert flushed == [run.id]


class _StoringJournal(ParkedJournal):
    @override
    def record(self, run: Run) -> bool:
        return True

    @override
    def advance(self, run_id: str, last_seq: int) -> None:
        pass

    @override
    def forget(self, run_id: str) -> None:
        pass


async def test_shutdown_cancels_live_runs_and_keeps_parked_ones():
    # Live runs are cancelled while their stores are still up; a stored park is kept for
    # the next process to resume.
    reg = RunRegistry()
    reg.attach_parked_journal(_StoringJournal())
    started = asyncio.Event()

    async def running(_run: Run) -> None:
        started.set()
        _ = await asyncio.Event().wait()

    async def parking(run: Run) -> None:
        run.park(None)

    live = reg.submit(kind="chat", owner_id="operator", orchestrator=running)
    idle = reg.submit(kind="chat", owner_id="operator", orchestrator=parking)
    _ = await started.wait()
    await idle.wait()
    assert idle.status is RunStatus.awaiting_input

    await reg.shutdown()

    assert live.status is RunStatus.cancelled
    assert live.stream.closed
    assert idle.status is RunStatus.awaiting_input
    assert not idle.stream.closed


async def test_shutdown_cancels_a_parked_run_whose_park_was_not_stored():
    reg = RunRegistry()

    async def parking(run: Run) -> None:
        run.park(None)

    idle = reg.submit(kind="chat", owner_id="operator", orchestrator=parking)
    await idle.wait()

    await reg.shutdown()

    assert idle.status is RunStatus.cancelled and idle.stream.closed


def test_adj_duration_reports_a_fractional_bound_exactly():
    from runs.registry import adj_duration

    assert adj_duration(1800) == "30-minute"
    assert adj_duration(45) == "45-second"
    # round() is half-to-even and would turn 2.5 minutes into "2-minute".
    assert adj_duration(150) == "2.5-minute"
    assert adj_duration(90) == "1.5-minute"


def test_count_duration_reports_a_fractional_bound_exactly():
    from runs.registry import count_duration

    assert count_duration(120) == "2 minutes"
    assert count_duration(1) == "1 second"
    assert count_duration(150) == "2.5 minutes"
    assert count_duration(60) == "1 minute"
