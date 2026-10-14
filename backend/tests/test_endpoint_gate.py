"""The per-endpoint run gate: capacity, priority order, positions, and no deadlock."""

from __future__ import annotations

import asyncio
import contextvars

from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from runs import Endpoint, EndpointGate, Run, RunRegistry, RunStatus
from services.gated_model import EndpointGatedModel

ONE = Endpoint(id="ep", name="local", capacity=1)


async def _settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


async def _hold(gate: EndpointGate, kind: str, order: list[str], name: str, release: asyncio.Event):
    async with gate.lease(ONE, kind):
        order.append(name)
        await release.wait()


async def test_a_full_endpoint_queues_the_next_lease_until_the_first_ends():
    gate = EndpointGate()
    order: list[str] = []
    first, second = asyncio.Event(), asyncio.Event()
    a = asyncio.create_task(_hold(gate, "chat", order, "a", first))
    b = asyncio.create_task(_hold(gate, "chat", order, "b", second))
    await _settle()
    assert order == ["a"]
    assert gate.snapshot("ep") == (1, 1)
    first.set()
    await _settle()
    assert order == ["a", "b"]
    second.set()
    await asyncio.gather(a, b)
    assert gate.snapshot("ep") == (0, 0)


async def test_waiters_are_served_interactive_then_linked_then_background_in_arrival_order():
    gate = EndpointGate()
    order: list[str] = []
    hold = asyncio.Event()
    go = asyncio.Event()
    tasks = [asyncio.create_task(_hold(gate, "chat", order, "holder", hold))]
    await _settle()
    for kind, name in [
        ("job", "bg1"),
        ("wake", "linked1"),
        ("chat", "chat1"),
        ("job", "bg2"),
        ("chat", "chat2"),
    ]:
        tasks.append(asyncio.create_task(_hold(gate, kind, order, name, go)))
        await _settle()
    go.set()
    hold.set()
    await asyncio.gather(*tasks)
    assert order == ["holder", "chat1", "chat2", "linked1", "bg1", "bg2"]


async def test_a_waiter_hears_its_position_as_the_line_moves():
    gate = EndpointGate()
    heard: dict[str, list[int]] = {"x": [], "y": []}
    hold = asyncio.Event()

    async def wait(name: str, kind: str) -> None:
        async with gate.lease(ONE, kind, on_position=heard[name].append):
            pass

    holder = asyncio.create_task(_hold(gate, "chat", [], "holder", hold))
    await _settle()
    x = asyncio.create_task(wait("x", "job"))
    await _settle()
    y = asyncio.create_task(wait("y", "chat"))
    await _settle()
    assert heard == {"x": [1, 2], "y": [1]}
    hold.set()
    await asyncio.gather(holder, x, y)
    assert heard == {"x": [1, 2, 1], "y": [1]}


async def test_a_cancelled_waiter_leaves_the_line_and_a_granted_one_gives_its_slot_back():
    gate = EndpointGate()
    hold = asyncio.Event()
    holder = asyncio.create_task(_hold(gate, "chat", [], "holder", hold))
    await _settle()
    waiting = asyncio.create_task(_hold(gate, "chat", [], "w", asyncio.Event()))
    await _settle()
    waiting.cancel()
    await _settle()
    assert gate.snapshot("ep") == (1, 0)

    granted = asyncio.create_task(_hold(gate, "chat", [], "g", asyncio.Event()))
    await _settle()
    hold.set()
    await asyncio.sleep(0)
    granted.cancel()
    await asyncio.gather(holder, granted, return_exceptions=True)
    assert gate.snapshot("ep") == (0, 0)


async def test_an_unlimited_endpoint_never_queues_and_a_raised_capacity_admits_waiters():
    gate = EndpointGate()
    open_ended = Endpoint(id="ep", name="local", capacity=None)
    async with gate.lease(open_ended, "chat"), gate.lease(open_ended, "chat"):
        assert gate.snapshot("ep") == (2, 0)

    order: list[str] = []
    hold = asyncio.Event()
    holder = asyncio.create_task(_hold(gate, "chat", order, "holder", hold))
    await _settle()
    waiter = asyncio.create_task(_hold(gate, "chat", order, "waiter", hold))
    await _settle()
    assert order == ["holder"]
    async with gate.lease(Endpoint(id="ep", name="local", capacity=3), "chat"):
        await _settle()
        assert order == ["holder", "waiter"]
    hold.set()
    await asyncio.gather(holder, waiter)


async def test_a_request_inside_the_lease_passes_through_and_one_outside_waits():
    gate = EndpointGate()
    ran: list[str] = []

    async def request(name: str) -> None:
        async with gate.request(ONE):
            ran.append(name)

    async with gate.lease(ONE, "chat"):
        await request("own")
        inherited = asyncio.create_task(request("inherited"))
        outsider = asyncio.create_task(request("outsider"), context=contextvars.Context())
        await _settle()
        assert ran == ["own", "inherited"]
    await asyncio.gather(inherited, outsider)
    assert ran == ["own", "inherited", "outsider"]


async def test_a_task_that_outlives_the_lease_waits_like_anyone_else():
    gate = EndpointGate()
    lease_over = asyncio.Event()
    ran: list[str] = []

    async def straggler() -> None:
        await lease_over.wait()
        async with gate.request(ONE):
            ran.append("straggler")

    hold = asyncio.Event()
    async with gate.lease(ONE, "chat"):
        leftover = asyncio.create_task(straggler())
    holder = asyncio.create_task(_hold(gate, "chat", [], "holder", hold))
    await _settle()
    lease_over.set()
    await _settle()
    assert ran == []
    hold.set()
    await asyncio.gather(holder, leftover)
    assert ran == ["straggler"]


async def test_a_request_slot_can_be_given_back_from_another_task():
    gate = EndpointGate()
    slot = gate.request(ONE)
    await asyncio.create_task(slot.__aenter__())
    queued = asyncio.create_task(gate.request(ONE).__aenter__())
    await _settle()
    assert not queued.done()
    await asyncio.create_task(slot.__aexit__(None, None, None))
    await _settle()
    assert queued.done()


async def test_a_gated_model_waits_for_its_endpoint_outside_a_lease():
    gate = EndpointGate()

    def answer(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart("ok")])

    agent = Agent(EndpointGatedModel(FunctionModel(answer), gate, ONE))

    async def ask() -> str:
        return (await agent.run("hi")).output

    async with gate.lease(ONE, "chat"):
        assert await ask() == "ok"
        outside = asyncio.create_task(ask(), context=contextvars.Context())
        await _settle()
        assert not outside.done()
    assert await outside == "ok"


def _task(run: Run) -> asyncio.Task[None]:
    assert run.task is not None
    return run.task


def _registry_run(reg: RunRegistry, kind: str, conversation: str, orchestrator):
    return reg.submit(
        kind=kind,
        owner_id="o",
        orchestrator=orchestrator,
        conversation_id=conversation,
        endpoint=ONE,
    )


async def test_a_parent_and_its_child_on_a_one_slot_endpoint_both_finish():
    reg = RunRegistry()
    finished: list[str] = []
    child_runs = []

    async def woken(run) -> None:
        finished.append("parent woken")

    async def child(run) -> None:
        finished.append("child")
        _registry_run(reg, "wake", "parent", woken)

    async def parent(run) -> None:
        child_runs.append(_registry_run(reg, "linked", "child", child))
        await _settle()
        finished.append("parent")

    first = _registry_run(reg, "chat", "parent", parent)
    await _task(first)
    await _task(child_runs[0])
    wake = reg.active_run_for("parent", "o")
    assert wake is not None
    await _task(wake)
    assert finished == ["parent", "child", "parent woken"]
    assert child_runs[0].status is RunStatus.done
    assert [e.body.type for e in child_runs[0].stream.replay()][:2] == ["run.queued", "run.started"]


async def test_three_queued_runs_start_in_priority_order_and_report_their_place():
    reg = RunRegistry()
    order: list[str] = []
    hold = asyncio.Event()

    async def holder(run) -> None:
        await hold.wait()

    def note(name: str):
        async def orchestrator(run) -> None:
            order.append(name)

        return orchestrator

    first = _registry_run(reg, "chat", "c0", holder)
    await _settle()
    runs = [
        _registry_run(reg, "job", "c1", note("routine")),
        _registry_run(reg, "wake", "c2", note("wake")),
        _registry_run(reg, "chat", "c3", note("operator")),
    ]
    await _settle()
    assert all(r.status is RunStatus.queued for r in runs)
    queued = [e.body for e in runs[0].stream.replay() if e.body.type == "run.queued"]
    assert [q.position for q in queued] == [1, 2, 3]
    assert queued[-1].endpoint == "local"
    hold.set()
    await asyncio.gather(_task(first), *(_task(r) for r in runs))
    assert order == ["operator", "wake", "routine"]
