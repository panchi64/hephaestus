"""How many runs a model endpoint serves at once, and who waits for it.

A local server with one slot serves one request at a time whatever the app does; without a
gate the rest queue invisibly inside the server, in arrival order, while each one evicts
the prompt cache the last one built. The gate moves that queue into the app, where it has
an order and a position the operator can see.

**A run leases its primary endpoint for its whole turn**, not per request. A turn is many
requests over one growing prefix; letting another thread's request in between them costs
the turn its cached prefix, which on a single-slot server is a full re-prefill per step.

**Requests outside a lease queue one at a time** (titles, summaries, the reviewer, a fold).
A request made from inside a run that already holds that endpoint passes straight through:
the run's own utility call must not wait for the run's own lease.

**Order is by lane, then arrival**: the operator's own turns and routine runs, then threads
the agents opened for themselves, then other unattended work. A saturated endpoint queues;
it never falls through to a fallback endpoint, because a fallback chain is for failures,
not for load.

Deadlock-free by construction as long as no run awaits another run, which delegation keeps:
spawning and messaging return at once and replies arrive as wakes.
"""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from contextvars import Context, ContextVar, copy_context
from dataclasses import dataclass, field, replace

from .lanes import Lane, lane_for

_RANK: dict[Lane, int] = {"interactive": 0, "routine": 0, "linked": 1, "background": 2}


@dataclass(frozen=True)
class Endpoint:
    """One endpoint as the gate sees it. ``capacity`` None ⇒ unlimited."""

    id: str
    name: str
    capacity: int | None


@dataclass
class _Lease:
    endpoint_id: str
    rank: int
    active: bool = True


_LEASE: ContextVar[_Lease | None] = ContextVar("endpoint_lease", default=None)


def outside_lease() -> Context:
    """A copy of the current context whose requests take a slot of their own, at the
    rank of the lease held here. For a task that may outlive that lease: passing through
    it would leave a request on the endpoint after the lease is handed to the next run."""
    context = copy_context()
    held = context.get(_LEASE)
    if held is not None:
        _ = context.run(_LEASE.set, replace(held, active=False))
    return context


@dataclass(order=True)
class _Waiter:
    rank: int
    seq: int
    granted: asyncio.Future[None] = field(compare=False)
    on_position: Callable[[int], None] | None = field(compare=False, default=None)
    position: int = field(compare=False, default=0)


@dataclass
class _Queue:
    capacity: int | None
    running: int = 0
    waiters: list[_Waiter] = field(default_factory=list)

    def has_room(self) -> bool:
        return self.capacity is None or self.running < self.capacity


class EndpointGate:
    def __init__(self) -> None:
        self._queues: dict[str, _Queue] = {}
        self._seq: itertools.count[int] = itertools.count()

    def snapshot(self, endpoint_id: str) -> tuple[int, int]:
        """Running and waiting counts for one endpoint."""
        queue = self._queues.get(endpoint_id)
        return (queue.running, len(queue.waiters)) if queue else (0, 0)

    @asynccontextmanager
    async def lease(
        self,
        endpoint: Endpoint | None,
        kind: str,
        *,
        on_position: Callable[[int], None] | None = None,
    ) -> AsyncGenerator[None]:
        """Hold ``endpoint`` for the block, waiting in line for it first. Requests made
        inside the block (and in tasks it starts) to the same endpoint pass through."""
        if endpoint is None:
            yield
            return
        rank = _RANK[lane_for(kind)]
        await self._acquire(endpoint, rank, on_position)
        held = _Lease(endpoint.id, rank)
        token = _LEASE.set(held)
        try:
            yield
        finally:
            held.active = False
            _LEASE.reset(token)
            self._release(endpoint.id)

    @asynccontextmanager
    async def request(self, endpoint: Endpoint) -> AsyncGenerator[None]:
        """Hold one slot of ``endpoint`` for one model request. The block may be exited
        on another task than the one that entered it, as a streamed response is."""
        held = _LEASE.get()
        if held is not None and held.active and held.endpoint_id == endpoint.id:
            yield
            return
        rank = held.rank if held is not None else _RANK["background"]
        await self._acquire(endpoint, rank, None)
        try:
            yield
        finally:
            self._release(endpoint.id)

    async def _acquire(
        self, endpoint: Endpoint, rank: int, on_position: Callable[[int], None] | None
    ) -> None:
        queue = self._queues.setdefault(endpoint.id, _Queue(endpoint.capacity))
        if queue.capacity != endpoint.capacity:
            queue.capacity = endpoint.capacity
            self._grant(queue)
        if queue.has_room() and not queue.waiters:
            queue.running += 1
            return
        waiter = _Waiter(
            rank, next(self._seq), asyncio.get_running_loop().create_future(), on_position
        )
        queue.waiters.append(waiter)
        queue.waiters.sort()
        self._announce(queue)
        try:
            await waiter.granted
        except asyncio.CancelledError:
            if waiter.granted.done() and not waiter.granted.cancelled():
                self._release(endpoint.id)
            else:
                queue.waiters.remove(waiter)
                self._announce(queue)
            raise

    def _release(self, endpoint_id: str) -> None:
        queue = self._queues[endpoint_id]
        queue.running -= 1
        self._grant(queue)

    def _grant(self, queue: _Queue) -> None:
        granted = False
        while queue.waiters and queue.has_room():
            waiter = queue.waiters.pop(0)
            queue.running += 1
            waiter.granted.set_result(None)
            granted = True
        if granted:
            self._announce(queue)

    @staticmethod
    def _announce(queue: _Queue) -> None:
        for index, waiter in enumerate(queue.waiters, start=1):
            if waiter.position != index:
                waiter.position = index
                if waiter.on_position is not None:
                    waiter.on_position(index)
