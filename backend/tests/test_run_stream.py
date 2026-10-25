"""RunStream: seq stamping, buffer replay, live fan-out, resume, close, unwatched."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

from core.shutdown import begin_shutdown, reset_shutdown
from runs import Event, RunStream
from runs.events import AnswerDelta

from ._helpers import leave_stream, watch_stream


def _texts(events: Sequence[Event]) -> list[str]:
    texts: list[str] = []
    for e in events:
        assert isinstance(e.body, AnswerDelta)
        texts.append(e.body.text)
    return texts


async def test_emit_assigns_monotonic_seq():
    stream = RunStream()
    a = stream.emit(AnswerDelta(text="a"))
    b = stream.emit(AnswerDelta(text="b"))
    assert (a.seq, b.seq) == (1, 2)
    assert stream.last_seq == 2


async def test_subscribe_after_close_replays_backlog_then_ends():
    stream = RunStream()
    _ = stream.emit(AnswerDelta(text="a"))
    _ = stream.emit(AnswerDelta(text="b"))
    stream.close()
    got = [e async for e in stream.subscribe()]
    assert _texts(got) == ["a", "b"]


async def test_resume_filters_by_after_seq():
    stream = RunStream()
    for t in ("a", "b", "c"):
        _ = stream.emit(AnswerDelta(text=t))
    stream.close()
    got = [e async for e in stream.subscribe(after_seq=1)]
    assert _texts(got) == ["b", "c"]
    assert [e.seq for e in got] == [2, 3]


async def test_live_subscriber_receives_emitted():
    stream = RunStream()
    received: list[Event] = []

    async def consume():
        async for event in stream.subscribe():
            received.append(event)

    task = asyncio.create_task(consume())
    await asyncio.sleep(0)  # let it register
    _ = stream.emit(AnswerDelta(text="a"))
    _ = stream.emit(AnswerDelta(text="b"))
    await asyncio.sleep(0)
    stream.close()
    await task
    assert _texts(received) == ["a", "b"]


async def test_backlog_then_live_no_gap_no_duplicate():
    stream = RunStream()
    _ = stream.emit(AnswerDelta(text="a"))  # buffered before subscribe
    received: list[Event] = []

    async def consume():
        async for event in stream.subscribe():
            received.append(event)

    task = asyncio.create_task(consume())
    await asyncio.sleep(0)
    _ = stream.emit(AnswerDelta(text="b"))  # arrives live
    await asyncio.sleep(0)
    stream.close()
    await task
    assert _texts(received) == ["a", "b"]
    assert [e.seq for e in received] == [1, 2]


async def test_slow_subscriber_is_dropped_not_grown_unbounded():
    # A consumer that never drains is evicted once its queue fills, instead of
    # letting emit() buffer for it without bound.
    from runs.stream import SUBSCRIBER_QUEUE_MAX

    stream = RunStream()

    async def stalled():  # registers, then never reads
        async for _ in stream.subscribe():
            await asyncio.sleep(3600)

    task = asyncio.create_task(stalled())
    await asyncio.sleep(0)  # let it register and park on queue.get()
    assert stream.subscriber_count == 1

    for i in range(SUBSCRIBER_QUEUE_MAX + 50):  # flood past the cap (no yields)
        _ = stream.emit(AnswerDelta(text=str(i)))

    # The wedged subscriber was evicted from the live set rather than ballooning.
    assert stream.subscriber_count == 0
    _ = task.cancel()


async def test_subscriber_count_tracks_live_registration_and_departure():
    stream = RunStream()
    assert stream.subscriber_count == 0

    async def consume():
        async for _ in stream.subscribe():
            pass

    task = asyncio.create_task(consume())
    await asyncio.sleep(0)  # let it register
    assert stream.subscriber_count == 1

    _ = stream.emit(AnswerDelta(text="a"))
    stream.close()
    await task
    assert stream.subscriber_count == 0


async def test_fanout_to_multiple_subscribers():
    stream = RunStream()
    out_a: list[Event] = []
    out_b: list[Event] = []

    async def consume(sink: list[Event]):
        async for event in stream.subscribe():
            sink.append(event)

    tasks = [asyncio.create_task(consume(out_a)), asyncio.create_task(consume(out_b))]
    await asyncio.sleep(0)
    _ = stream.emit(AnswerDelta(text="x"))
    await asyncio.sleep(0)
    stream.close()
    _ = await asyncio.gather(*tasks)
    assert _texts(out_a) == ["x"]
    assert _texts(out_b) == ["x"]


async def test_when_unwatched_fires_once_on_the_last_detach():
    stream = RunStream()
    fired: list[int] = []
    stream.when_unwatched(lambda: fired.append(1))
    first, second = await watch_stream(stream), await watch_stream(stream)

    await leave_stream(first)
    assert fired == []
    await leave_stream(second)
    assert fired == [1]

    await leave_stream(await watch_stream(stream))
    assert fired == [1]


async def test_a_tailing_subscriber_is_not_a_watcher():
    stream = RunStream()
    fired: list[int] = []
    stream.when_unwatched(lambda: fired.append(1))
    watcher = await watch_stream(stream)
    tail = await watch_stream(stream, watching=False)
    assert (stream.subscriber_count, stream.watcher_count) == (2, 1)

    await leave_stream(watcher)
    assert fired == [1]
    await leave_stream(tail)
    assert fired == [1]


async def test_a_failing_unwatched_callback_does_not_stop_the_rest():
    stream = RunStream()
    fired: list[int] = []

    def boom() -> None:
        raise RuntimeError("boom")

    stream.when_unwatched(boom)
    stream.when_unwatched(lambda: fired.append(1))

    await leave_stream(await watch_stream(stream))
    assert fired == [1]


async def test_when_unwatched_ignores_close():
    stream = RunStream()
    fired: list[int] = []
    stream.when_unwatched(lambda: fired.append(1))
    task = await watch_stream(stream)

    stream.close()
    await task
    assert fired == []


async def test_when_unwatched_ignores_shutdown():
    stream = RunStream()
    fired: list[int] = []
    stream.when_unwatched(lambda: fired.append(1))
    task = await watch_stream(stream)

    begin_shutdown()
    try:
        await task
    finally:
        reset_shutdown()
    assert fired == []
