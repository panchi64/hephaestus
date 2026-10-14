"""``gather_bounded`` — the shared bounded fan-out behind a mail listing's per-message
fetches and a reaper's per-session teardowns."""

from __future__ import annotations

import asyncio

import pytest

from core.concurrency import gather_bounded


async def test_fan_out_never_exceeds_the_cap_but_shrinks_to_workload():
    running = 0
    peak = 0

    async def worker():
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0)
        running -= 1

    _ = await gather_bounded([worker() for _ in range(10)], 3)
    assert peak == 3  # capped

    running = peak = 0
    _ = await gather_bounded([worker() for _ in range(2)], 5)
    assert peak == 2  # only as many workers as workload, not the cap


async def test_results_come_back_in_input_order_not_completion_order():
    # The callers index the result against their input list — a mail listing pairs the
    # nth body with the nth id — so finishing order must not reorder anything.
    async def worker(n: int) -> int:
        await asyncio.sleep((5 - n) / 1000)  # later items finish first
        return n

    assert await gather_bounded([worker(n) for n in range(5)], 2) == [0, 1, 2, 3, 4]


async def test_a_nonsense_cap_still_runs_everything_serially():
    async def worker() -> int:
        return 1

    assert await gather_bounded([worker() for _ in range(3)], 0) == [1, 1, 1]


async def test_one_failure_propagates_rather_than_being_swallowed():
    # Isolation is the caller's call, made inside its own coroutine — the plumbing must
    # not decide for it by quietly returning a short list.
    async def ok() -> int:
        return 1

    async def boom() -> int:
        raise RuntimeError("upstream said no")

    with pytest.raises(RuntimeError, match="upstream said no"):
        _ = await gather_bounded([ok(), boom(), ok()], 2)
