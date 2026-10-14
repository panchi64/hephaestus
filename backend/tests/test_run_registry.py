"""RunRegistry: lifecycle, terminal mapping, bounds, cancellation, queueing."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest

from runs import ConversationBusyError, LaneLimits, Orchestrator, Run, RunRegistry, RunStatus
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
    # A run that measured nothing reports nothing: an empty metrics frame would read as
    # the authoritative state of the thread and blank its meter.
    assert _types(run) == ["run.started", "answer.delta", "run.ended"]
    assert _ended(run).outcome == "done"


async def test_metrics_the_run_measured_precede_the_closing_frame_on_every_path():
    # A stopped or failed turn still spent the tokens it spent.
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
        _ = await asyncio.Event().wait()  # never completes on its own

    run = reg.submit(kind="agent", owner_id="operator", orchestrator=orch)
    await asyncio.sleep(0)  # let it start
    assert await reg.cancel(run.id) is True
    await run.wait()

    assert run.status is RunStatus.cancelled
    assert _ended(run).outcome == "cancelled"
    # cancelling an already-terminal run is a no-op
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
        await asyncio.sleep(5)  # no further events → inactivity fires

    run = reg.submit(kind="research", owner_id="operator", orchestrator=orch)
    await run.wait()

    assert run.status is RunStatus.blocked
    events = run.stream.replay()
    notice = next(e.body for e in events if e.body.type == "limit.notice")
    assert notice.limit == "time"
    assert events[-1].body.type == "run.ended"
    assert events[-1].body.outcome == "blocked"


async def test_keepalive_holds_a_long_silent_call_open_past_the_inactivity_bound():
    # A non-streaming model call — a long report being generated — emits nothing between
    # phase boundaries. Without this the run would be stopped for idling while making real
    # progress, and every feature with a slow call would hand-roll its own heartbeat.
    reg = RunRegistry(wall_clock_timeout_s=5.0, inactivity_timeout_s=0.1)

    async def orch(run: Run) -> None:
        _ = run.emit(AnswerDelta(text="start"))
        async with run.keepalive():
            await asyncio.sleep(0.5)  # five times the bound, silent throughout

    run = reg.submit(kind="research", owner_id="operator", orchestrator=orch)
    await run.wait()

    assert run.status is RunStatus.done
    assert _ended(run).outcome == "done"


async def test_the_bound_applies_again_the_moment_the_call_returns():
    # The scope covers one awaited call, not the rest of the run: leaving it must not
    # leave the watchdog disarmed.
    reg = RunRegistry(wall_clock_timeout_s=5.0, inactivity_timeout_s=0.1)

    async def orch(run: Run) -> None:
        async with run.keepalive():
            await asyncio.sleep(0.3)
        await asyncio.sleep(5)  # unprotected again → inactivity fires

    run = reg.submit(kind="research", owner_id="operator", orchestrator=orch)
    await run.wait()

    assert run.status is RunStatus.blocked
    notice = next(e.body for e in run.stream.replay() if e.body.type == "limit.notice")
    assert notice.limit == "time"


async def test_keepalive_beats_inside_a_bound_shorter_than_its_default_interval():
    # The interval is a ceiling, not a fixed rate: a run held to a tight inactivity bound
    # gets a proportionally faster heartbeat, so a shortened timeout can't outrun it.
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
    # Both runs are the same kind, so they share one lane and the second waits.
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
    assert run2.status is RunStatus.queued  # blocked at the concurrency gate

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
    # A different conversation, a different owner, or an unknown id → no match.
    assert reg.active_run_for("other", "operator") is None
    assert reg.active_run_for("c1", "intruder") is None

    release.set()
    await run.wait()

    # A terminal run is no longer active — the conversation read shows no live run.
    assert reg.active_run_for("c1", "operator") is None


async def test_submit_rejects_a_second_run_for_the_same_conversation():
    # The atomic backstop behind the route-level 409 guard (chat-03/resume-02): submit
    # itself refuses a second run for a conversation that already has a live one, with
    # no `await` between the check and registering the new run — the race a route's own
    # earlier check (before its own awaits) can't fully close.
    reg = RunRegistry()
    started, release = asyncio.Event(), asyncio.Event()

    async def orch(_run: Run) -> None:
        started.set()
        _ = await release.wait()

    run = reg.submit(kind="chat", owner_id="operator", orchestrator=orch, conversation_id="c1")
    _ = await started.wait()

    with pytest.raises(ConversationBusyError):
        _ = reg.submit(kind="chat", owner_id="operator", orchestrator=orch, conversation_id="c1")

    # A different conversation or a different owner on the same id is unaffected.
    other_conv = reg.submit(
        kind="chat", owner_id="operator", orchestrator=orch, conversation_id="c2"
    )
    other_owner = reg.submit(
        kind="chat", owner_id="someone-else", orchestrator=orch, conversation_id="c1"
    )

    release.set()
    _ = await asyncio.gather(run.wait(), other_conv.wait(), other_owner.wait())

    # Once the live run reaches terminal, the conversation is free again.
    run2 = reg.submit(kind="chat", owner_id="operator", orchestrator=orch, conversation_id="c1")
    await run2.wait()
    assert run2.status is RunStatus.done


async def test_active_run_for_prefers_the_most_recent():
    # active_run_for's own tie-break (most-recent non-terminal wins) is exercised
    # directly against the registry's run table. `submit` itself now refuses to create a
    # second live run for the same conversation (see
    # `test_submit_rejects_a_second_run_for_the_same_conversation`), so two live
    # candidates for one conversation can only coexist via direct construction here —
    # this is testing `active_run_for`'s selection policy in isolation, not `submit`'s.
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

    newer.status = RunStatus.done  # terminal → excluded regardless of recency
    assert reg.active_run_for("c1", "operator") is older


async def test_claim_rejects_a_second_claim_on_the_same_conversation():
    # `claim` is the pre-submit/pre-mutation mutual exclusion regenerate/edit/delete
    # take before their own further `await`s (model resolve, orphan lookup) — a bare
    # `active_run_for` check can't see a claim that hasn't registered a run yet.
    reg = RunRegistry()
    reg.claim("c1", "operator")

    with pytest.raises(ConversationBusyError):
        reg.claim("c1", "operator")

    # A different conversation or a different owner on the same id is unaffected.
    reg.claim("c2", "operator")
    reg.claim("c1", "someone-else")

    reg.release("c1", "operator")
    reg.release("c2", "operator")
    reg.release("c1", "someone-else")

    # Freed after release — a fresh claim succeeds.
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

    # Once the run reaches terminal, a claim succeeds again.
    reg.claim("c1", "operator")
    reg.release("c1", "operator")


def test_release_is_idempotent_without_a_prior_claim():
    reg = RunRegistry()
    reg.release("never-claimed", "operator")  # must not raise


# --- on_terminal: the injected terminal-transition hook -----------------------------


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
        _ = await asyncio.Event().wait()  # never completes on its own

    run = reg.submit(kind="agent", owner_id="operator", orchestrator=orch)
    await asyncio.sleep(0)
    assert await reg.cancel(run.id) is True
    await run.wait()

    assert calls == [run]


async def test_on_terminal_not_fired_on_park_but_fires_on_cancel_while_parked():
    # Parking isn't a terminal transition — the hook must stay silent until the run
    # is actually decided (approved/denied/resumed to completion) or, here, cancelled
    # out of band while still parked (the only close() path that bypasses `_execute`).
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
    await run.wait()  # must not raise, and must not affect the run's own outcome

    assert run.status is RunStatus.done
    assert "on_terminal hook failed" in caplog.text


async def test_on_terminal_reads_the_live_subscriber_count_before_close():
    # The subscriber count must reflect "was anyone watching" at the moment of the
    # terminal transition — captured synchronously by the hook, before the stream
    # (and thus each subscriber's own cleanup) has had any chance to run.
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
    await asyncio.sleep(0)  # let it register
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
    # A turn stays externally visible as `awaiting_input` while its orchestrator finishes
    # its own post-park work (settling the concurrent title namer). A cancel landing in
    # that window closes the stream; the orchestrator then returns into `_execute`, which
    # must not emit onto the closed stream — doing so flipped the recorded `cancelled`
    # outcome to `error` and fired the terminal hook a second time.
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
    assert calls == [run]  # exactly once
    assert _types(run)[-1] == "run.ended"
    assert _ended(run).outcome == "cancelled"


async def test_a_bound_tripping_just_after_a_park_still_flushes_the_parked_turn():
    # `on_timeout` is disarmed at the park, so a bound tripping before the orchestrator
    # has finished unwinding would otherwise block the run with the parked turn never
    # persisted — the operator's own prompt included.
    flushed: list[str] = []
    reg = RunRegistry(inactivity_timeout_s=0.05)

    async def orch(run: Run) -> None:
        run.park(None)
        run.on_timeout = None  # what the engine does once a turn has parked
        run.on_park_cancel = lambda: flushed.append(run.id)
        _ = await asyncio.Event().wait()  # never returns; the bound trips here

    run = reg.submit(kind="chat", owner_id="operator", orchestrator=orch)
    await run.wait()

    assert run.status is RunStatus.blocked
    assert flushed == [run.id]


async def test_shutdown_cancels_live_runs_so_their_waiters_settle():
    # Nothing else cancels an in-flight or parked run at shutdown, and the stores each run
    # writes through are torn down right after — so a turn still executing would submit
    # onto a stopped drainer and be discarded with no error. Registered last in the app's
    # lifecycle (so it stops first), this gives every run its pre-cancel flush while those
    # stores are still live, and settles anything awaiting the run's terminal transition.
    reg = RunRegistry()
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
    assert idle.status is RunStatus.cancelled
    assert live.stream.closed and idle.stream.closed


# --- duration formatting: exact minutes, no silent rounding --------------------------


def testadj_duration_reports_a_fractional_bound_exactly():
    from runs.registry import adj_duration

    assert adj_duration(1800) == "30-minute"
    assert adj_duration(45) == "45-second"
    # Python's round() is round-half-to-even (round(2.5) == 2) — a naive round()
    # would understate a 150s/2.5-minute bound as "2-minute"; this reports the
    # operator's actual configured duration instead.
    assert adj_duration(150) == "2.5-minute"
    assert adj_duration(90) == "1.5-minute"


def testcount_duration_reports_a_fractional_bound_exactly():
    from runs.registry import count_duration

    assert count_duration(120) == "2 minutes"
    assert count_duration(1) == "1 second"
    assert count_duration(150) == "2.5 minutes"
    assert count_duration(60) == "1 minute"
