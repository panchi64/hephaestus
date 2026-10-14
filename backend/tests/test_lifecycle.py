"""The harness lifecycle registry — start now, stop in reverse, never lose a stop."""

from __future__ import annotations

import asyncio

import pytest

from harness import LifecycleRegistry


async def test_starts_run_immediately_and_stops_reverse_order():
    """`start` brings the unit up at its call site (construction-point timing), and
    `stop_all` unwinds last-registered-first — the property the app lifespan relies
    on so a dependency built earlier outlives everything built on top of it."""
    log: list[str] = []
    registry = LifecycleRegistry()

    async def unit(name: str) -> None:
        log.append(f"start:{name}")

    async def stop(name: str) -> None:
        log.append(f"stop:{name}")

    await registry.start("a", start=lambda: unit("a"), stop=lambda: stop("a"))
    assert log == ["start:a"]  # started before the next unit is even constructed
    await registry.start("b", start=lambda: unit("b"), stop=lambda: stop("b"))
    registry.on_stop("c", lambda: stop("c"))
    await registry.stop_all()
    assert log == ["start:a", "start:b", "stop:c", "stop:b", "stop:a"]


async def test_sync_stop_callables_are_supported():
    """A unit whose teardown is synchronous (cancel in-flight work, close a handle)
    registers the plain callable — no async wrapper boilerplate at the call site."""
    stopped: list[str] = []
    registry = LifecycleRegistry()
    registry.on_stop("sync",lambda: stopped.append("sync"))
    await registry.stop_all()
    assert stopped == ["sync"]


async def test_one_failing_stop_never_blocks_the_rest():
    """Teardown is best-effort per unit: a stop that raises is logged and the
    remaining units still stop — shutdown must never wedge on one bad capability."""
    stopped: list[str] = []
    registry = LifecycleRegistry()
    registry.on_stop("innermost", lambda: stopped.append("innermost"))

    def explode() -> None:
        raise RuntimeError("boom")

    registry.on_stop("faulty", explode)
    registry.on_stop("outermost", lambda: stopped.append("outermost"))
    await registry.stop_all()
    assert stopped == ["outermost", "innermost"]


async def test_start_failure_propagates_and_registers_no_stop():
    """A capability that can't come up fails the boot loudly; its stop is never
    recorded, so the unwind only covers what actually started."""
    stopped: list[str] = []
    registry = LifecycleRegistry()
    registry.on_stop("prior", lambda: stopped.append("prior"))

    async def bad_start() -> None:
        raise RuntimeError("no runtime")

    with pytest.raises(RuntimeError):
        await registry.start("bad", start=bad_start, stop=lambda: stopped.append("bad"))
    await registry.stop_all()
    assert stopped == ["prior"]


async def test_tracked_task_is_cancelled_and_awaited_at_stop():
    """A fire-and-forget startup task still pending at shutdown is cancelled *and*
    awaited, so it can never warn as destroyed-while-pending."""
    parked = asyncio.Event()
    cancelled = asyncio.Event()

    async def forever() -> None:
        parked.set()
        try:
            _ = await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    registry = LifecycleRegistry()
    task = registry.track("parked", forever())
    _ = await parked.wait()
    await registry.stop_all()
    assert cancelled.is_set()
    assert task.cancelled()


async def test_tracked_task_already_done_is_a_noop_at_stop():
    registry = LifecycleRegistry()

    async def quick() -> None:
        return None

    task = registry.track("quick", quick())
    await task
    await registry.stop_all()  # must not raise or hang


async def test_stop_all_is_idempotent():
    stopped: list[str] = []
    registry = LifecycleRegistry()
    registry.on_stop("once", lambda: stopped.append("once"))
    await registry.stop_all()
    await registry.stop_all()
    assert stopped == ["once"]
