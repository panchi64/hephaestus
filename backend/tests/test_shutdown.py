"""The shutdown contract: bounded stops, and long-lived streams that end on the signal."""

from __future__ import annotations

import asyncio
import time

import pytest

from core.shutdown import (
    STOPPED,
    begin_shutdown,
    ends_on_shutdown,
    get_unless_shutdown,
    is_shutting_down,
    reset_shutdown,
    wait_shutdown,
)
from core.sse import sse_stream
from core.tasks import cancel_and_wait
from core.worker import WriteBehindWorker
from runs.stream import RunStream


@pytest.fixture(autouse=True)
def _clear_signal():
    reset_shutdown()
    yield
    reset_shutdown()


async def test_cancel_and_wait_returns_once_the_task_unwinds():
    cleaned = asyncio.Event()

    async def work():
        try:
            await asyncio.sleep(60)
        finally:
            cleaned.set()

    task = asyncio.create_task(work())
    await asyncio.sleep(0)
    await cancel_and_wait(task, 1.0)
    assert task.done() and cleaned.is_set()


async def test_cancel_and_wait_gives_up_on_a_task_that_ignores_the_cancel():
    release = asyncio.Event()

    async def stubborn():
        while not release.is_set():
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                continue  # swallows the cancel

    task = asyncio.create_task(stubborn())
    await asyncio.sleep(0)
    started = time.monotonic()
    await cancel_and_wait(task, 0.2)
    assert time.monotonic() - started < 1.0
    assert not task.done()
    release.set()
    task.cancel()
    await asyncio.wait({task}, timeout=1.0)


async def test_cancel_and_wait_accepts_none_and_finished_tasks():
    await cancel_and_wait(None, 0.1)
    task = asyncio.create_task(asyncio.sleep(0))
    await task
    await cancel_and_wait(task, 0.1)


async def test_worker_stop_respects_its_deadline_with_a_stuck_job():
    started = asyncio.Event()

    async def stuck(_item: int) -> None:
        started.set()
        await asyncio.sleep(60)

    worker = WriteBehindWorker(stuck, name="stuck")
    await worker.start()
    worker.submit(1)
    worker.submit(2)
    await started.wait()
    began = time.monotonic()
    await worker.stop(drain_timeout_s=0.2)
    assert time.monotonic() - began < 2.0


async def test_worker_stop_still_flushes_what_it_can():
    seen: list[int] = []

    async def handler(item: int) -> None:
        seen.append(item)

    worker = WriteBehindWorker(handler, name="flush")
    await worker.start()
    for i in range(5):
        worker.submit(i)
    await worker.stop(drain_timeout_s=2.0)
    assert seen == [0, 1, 2, 3, 4]


async def test_wait_shutdown_wakes_on_begin_and_is_immediate_afterwards():
    waiter = asyncio.create_task(wait_shutdown())
    await asyncio.sleep(0)
    assert not waiter.done()
    begin_shutdown()
    await asyncio.wait_for(waiter, 1.0)
    assert is_shutting_down()
    await asyncio.wait_for(wait_shutdown(), 1.0)


async def test_wait_shutdown_wakes_when_raised_from_another_thread():
    waiter = asyncio.create_task(wait_shutdown())
    await asyncio.sleep(0)
    await asyncio.to_thread(begin_shutdown)
    await asyncio.wait_for(waiter, 1.0)


async def test_get_unless_shutdown_yields_items_times_out_and_stops():
    queue: asyncio.Queue[int] = asyncio.Queue()
    queue.put_nowait(7)
    assert await get_unless_shutdown(queue, 1.0) == 7
    with pytest.raises(TimeoutError):
        await get_unless_shutdown(queue, 0.05)
    asyncio.get_running_loop().call_later(0.05, begin_shutdown)
    assert await asyncio.wait_for(get_unless_shutdown(queue), 1.0) is STOPPED


async def test_ends_on_shutdown_closes_the_source():
    closed = asyncio.Event()

    async def source():
        try:
            while True:
                await asyncio.sleep(0.01)
                yield b"x"
        finally:
            closed.set()

    seen = 0
    async for _ in ends_on_shutdown(source()):
        seen += 1
        if seen == 2:
            begin_shutdown()
    assert closed.is_set()


async def test_run_stream_subscriber_ends_on_shutdown_though_the_run_never_closes():
    stream = RunStream()
    ended = asyncio.Event()

    async def consume():
        async for _ in stream.subscribe():
            pass
        ended.set()

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.05)
    assert not ended.is_set()
    begin_shutdown()
    await asyncio.wait_for(ended.wait(), 1.0)
    await task


async def test_sse_response_ends_on_shutdown_instead_of_pinging_forever():
    async def never_ends():
        while True:
            await asyncio.sleep(3600)
            yield 1

    response = sse_stream(never_ends, lambda item: f"data: {item}\n\n")
    frames = response.body_iterator.__aiter__()
    asyncio.get_running_loop().call_later(0.05, begin_shutdown)

    async def drain():
        return [frame async for frame in frames]

    assert await asyncio.wait_for(drain(), 2.0) == []
