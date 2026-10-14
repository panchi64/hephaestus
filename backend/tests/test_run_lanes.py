"""Lanes — the operator's turn never queues behind unattended work.

The bug this replaces was invisible rather than loud: one pool of eight slots, a
scheduled task and a few agent-opened background threads filling it, and the operator's
next message sitting at the gate looking exactly like a slow model. So what is pinned
here is the *separation*, not the numbers — that a saturated autonomous lane leaves the
interactive one untouched, and that every kind the composition path actually submits
under lands where it says it does.
"""

from __future__ import annotations

import asyncio

from runs import (
    CHAT_TURN_KINDS,
    LANE_BY_KIND,
    LaneLimits,
    Orchestrator,
    Run,
    RunRegistry,
    RunStatus,
    lane_for,
)
from runs.lanes import DEFAULT_LANE, LANES


def _blocking(started: asyncio.Event, release: asyncio.Event) -> Orchestrator:
    async def orchestrator(_run: Run) -> None:
        started.set()
        _ = await release.wait()

    return orchestrator


async def test_an_unknown_kind_waits_in_the_unattended_lane():
    # A kind nobody mapped is far likelier to be new autonomous work than a turn someone
    # is waiting on, so the fallback must never be the operator's lane.
    assert lane_for("chat") == "interactive"
    # A routine runs on the operator's schedule, in slots of its own.
    assert lane_for("task") == "routine"
    assert lane_for("linked") == "linked"
    assert lane_for("something-invented-later") == "background"


def test_every_composed_kind_has_a_lane():
    # A chat turn without a lane would silently inherit the unattended fallback, so every
    # composed kind must be in the map. The converse is *not* asserted: the map also
    # carries kinds that are not turns (a hand-started fold), and equating the two is what
    # would enrol the next one as a chat turn without anyone choosing that.
    assert CHAT_TURN_KINDS <= frozenset(LANE_BY_KIND)
    assert set(LANE_BY_KIND.values()) | {DEFAULT_LANE} == set(LANES)


def test_a_fold_has_a_lane_and_is_not_a_chat_turn():
    """The operator's own fold waits in the interactive lane — they pressed a button and
    are watching — but it is not a turn: nothing composes it, and a chat kind is what
    decides whether an orchestrator drains the queued-message inbox. A fold that drained
    one would swallow a message the operator sent while it ran."""
    assert lane_for("compaction") == "interactive"
    assert "compaction" not in CHAT_TURN_KINDS


async def test_a_saturated_background_lane_does_not_queue_the_operators_turn():
    # Three slots on the host, one per lane: the operator's is the one the unattended
    # lanes cannot reach, which is the whole guarantee — stated as a share of a ceiling,
    # because a lane that could add slots to the host would be buying it with resources.
    reg = RunRegistry(lanes=LaneLimits(total=3, interactive=1, background=1, linked=1))
    task_started, release_task = asyncio.Event(), asyncio.Event()
    linked_started, release_linked = asyncio.Event(), asyncio.Event()
    chat_started, release_chat = asyncio.Event(), asyncio.Event()

    task = reg.submit(
        kind="job", owner_id="operator", orchestrator=_blocking(task_started, release_task)
    )
    linked = reg.submit(
        kind="linked", owner_id="operator", orchestrator=_blocking(linked_started, release_linked)
    )
    second_task = reg.submit(
        kind="job", owner_id="operator", orchestrator=_blocking(asyncio.Event(), release_task)
    )
    _ = await task_started.wait()
    _ = await linked_started.wait()

    chat = reg.submit(
        kind="chat", owner_id="operator", orchestrator=_blocking(chat_started, release_chat)
    )
    # The interactive lane is empty, so this starts while both unattended lanes are full
    # and a second task is queued behind the first.
    _ = await asyncio.wait_for(chat_started.wait(), timeout=1.0)
    assert chat.status is RunStatus.running
    assert second_task.status is RunStatus.queued

    release_chat.set()
    release_task.set()
    release_linked.set()
    for run in (chat, task, linked, second_task):
        await run.wait()
    assert all(run.status is RunStatus.done for run in (chat, task, linked, second_task))


async def test_two_interactive_turns_still_share_their_own_lane():
    # Splitting the pool must not accidentally make the operator's lane unbounded.
    reg = RunRegistry(lanes=LaneLimits(interactive=1))
    first_started, release = asyncio.Event(), asyncio.Event()

    first = reg.submit(
        kind="chat", owner_id="operator", orchestrator=_blocking(first_started, release)
    )
    second = reg.submit(
        kind="chat", owner_id="operator", orchestrator=_blocking(asyncio.Event(), release)
    )
    _ = await first_started.wait()
    assert second.status is RunStatus.queued

    release.set()
    await first.wait()
    await second.wait()


# --- the ceiling: lanes redistribute it, they never add to it ----------------
def test_the_default_lanes_leave_the_operator_a_floor_inside_one_ceiling():
    limits = LaneLimits()
    # The point of the split, stated as arithmetic: peak concurrency is the ceiling, not
    # the sum of the lanes, and what protects the operator is how little the unattended
    # lanes may take of it.
    assert limits.ceiling == limits.interactive
    assert limits.background + limits.linked < limits.ceiling
    assert limits.attended_floor == limits.ceiling - limits.background - limits.linked


async def test_a_lane_cannot_push_the_host_past_its_ceiling():
    # Two lanes, four slots between them, but a ceiling of two: a third run waits even
    # though its own lane is empty. Lanes decide *who* holds the host's slots; only the
    # ceiling decides how many there are.
    reg = RunRegistry(lanes=LaneLimits(total=2, interactive=2, background=2, linked=1))
    release = asyncio.Event()
    started = [asyncio.Event() for _ in range(3)]

    runs = [
        reg.submit(kind="job", owner_id="operator", orchestrator=_blocking(started[0], release)),
        reg.submit(kind="job", owner_id="operator", orchestrator=_blocking(started[1], release)),
        reg.submit(kind="chat", owner_id="operator", orchestrator=_blocking(started[2], release)),
    ]
    _ = await started[0].wait()
    _ = await started[1].wait()
    await asyncio.sleep(0)  # let the third run reach its gate

    assert not started[2].is_set()
    assert runs[2].status is RunStatus.queued

    release.set()
    for run in runs:
        await run.wait()
    assert all(run.status is RunStatus.done for run in runs)


# --- the permit is released on every exit, not only the happy one ------------
async def test_a_failed_run_releases_its_lane_permit():
    # A one-wide lane whose permit leaked on an exception would take the operator's lane
    # out of service for the life of the process, with nothing to see but queued turns.
    reg = RunRegistry(lanes=LaneLimits(interactive=1))

    async def boom(_run: Run) -> None:
        raise RuntimeError("the model refused")

    failed = reg.submit(kind="chat", owner_id="operator", orchestrator=boom)
    await failed.wait()
    assert failed.status is RunStatus.error

    started, release = asyncio.Event(), asyncio.Event()
    after = reg.submit(kind="chat", owner_id="operator", orchestrator=_blocking(started, release))
    _ = await asyncio.wait_for(started.wait(), timeout=1.0)

    release.set()
    await after.wait()


async def test_a_cancelled_run_releases_its_lane_permit():
    # Stopping a turn is the most-used control in the app; a permit lost to it would
    # narrow the lane a little further every time the operator pressed it.
    reg = RunRegistry(lanes=LaneLimits(interactive=1))
    first_started, never = asyncio.Event(), asyncio.Event()

    first = reg.submit(
        kind="chat", owner_id="operator", orchestrator=_blocking(first_started, never)
    )
    second_started, release = asyncio.Event(), asyncio.Event()
    second = reg.submit(
        kind="chat", owner_id="operator", orchestrator=_blocking(second_started, release)
    )
    _ = await first_started.wait()
    assert second.status is RunStatus.queued

    _ = await reg.cancel(first.id)
    await first.wait()
    assert first.status is RunStatus.cancelled

    _ = await asyncio.wait_for(second_started.wait(), timeout=1.0)
    release.set()
    await second.wait()
    assert second.status is RunStatus.done


async def test_a_run_cancelled_while_queued_releases_the_permit_it_was_waiting_for():
    # Cancellation delivered *at the gate* unwinds a half-taken pair — the lane permit is
    # already held while the ceiling is still being waited on, and dropping it there is
    # the difference between a lane that recovers and one that quietly shrinks.
    reg = RunRegistry(lanes=LaneLimits(total=1, interactive=2))
    first_started, release_first = asyncio.Event(), asyncio.Event()

    first = reg.submit(
        kind="chat", owner_id="operator", orchestrator=_blocking(first_started, release_first)
    )
    waiting = reg.submit(
        kind="chat", owner_id="operator", orchestrator=_blocking(asyncio.Event(), asyncio.Event())
    )
    _ = await first_started.wait()
    assert waiting.status is RunStatus.queued

    _ = await reg.cancel(waiting.id)
    await waiting.wait()
    release_first.set()
    await first.wait()

    third_started, release_third = asyncio.Event(), asyncio.Event()
    third = reg.submit(
        kind="chat", owner_id="operator", orchestrator=_blocking(third_started, release_third)
    )
    _ = await asyncio.wait_for(third_started.wait(), timeout=1.0)
    release_third.set()
    await third.wait()


async def test_routines_run_in_reserved_slots_beside_a_full_ceiling():
    # One slot on the host, held by the operator: a routine still starts, in its own slot,
    # and a second routine waits only for the routine lane.
    reg = RunRegistry(lanes=LaneLimits(total=1, interactive=1, routine=1))
    chat_started, routine_started, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    chat = reg.submit(
        kind="chat", owner_id="operator", orchestrator=_blocking(chat_started, release)
    )
    _ = await chat_started.wait()
    routine = reg.submit(
        kind="task", owner_id="operator", orchestrator=_blocking(routine_started, release)
    )
    _ = await asyncio.wait_for(routine_started.wait(), timeout=1.0)
    second = reg.submit(
        kind="task", owner_id="operator", orchestrator=_blocking(asyncio.Event(), release)
    )
    await asyncio.sleep(0)
    assert second.status is RunStatus.queued

    release.set()
    for run in (chat, routine, second):
        await run.wait()


async def test_routines_never_take_the_operators_slots():
    reg = RunRegistry(lanes=LaneLimits(total=1, interactive=1, routine=2))
    routines_started = [asyncio.Event(), asyncio.Event()]
    chat_started, release = asyncio.Event(), asyncio.Event()
    runs = [
        reg.submit(kind="task", owner_id="operator", orchestrator=_blocking(started, release))
        for started in routines_started
    ]
    for started in routines_started:
        _ = await started.wait()
    chat = reg.submit(
        kind="chat", owner_id="operator", orchestrator=_blocking(chat_started, release)
    )
    _ = await asyncio.wait_for(chat_started.wait(), timeout=1.0)

    release.set()
    for run in (*runs, chat):
        await run.wait()
