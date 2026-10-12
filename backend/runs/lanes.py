"""Which queue a run waits in.

One pool of eight slots shared by everything was a bound on the *host*, not a policy
about *whose work matters*: a scheduled task firing at nine and two sub-agents the agent
launched for itself could hold every slot, and the operator's next message sat at the
gate behind them — queued, silent, and indistinguishable from a slow model.

So the pool is **carved into lanes** rather than replaced by them: the host ceiling stays
one semaphore, and each lane holds a second, narrower one that caps how much of that
ceiling its kind of work may take. A run picks its lane from its own ``kind``, which is
the whole mechanism — no call site threads a new argument, because every submitter
already says what kind of work it is submitting.

**Lanes redistribute the ceiling; they never raise it.** Two semaphores rather than one
per lane because the alternative — a pool per lane — makes the machine's real limit the
*sum* of the lanes, which is how a change meant to lower resource use quietly raises peak
concurrency instead. The operator's protection does not come from a bigger pool: it comes
from the unattended lanes being capped well below the ceiling, so slots they can never
hold are, by arithmetic, always there for a turn somebody is waiting on.

**The lanes are about who is waiting, not what the run does.** All three kinds run the
same chat orchestrator over the same tools; they differ in whether somebody is sitting in
front of the answer. ``interactive`` is the operator's own turn and gets the widest lane.
``background`` is unattended work — a scheduled task, whose whole point is that nobody is
watching it. ``linked`` is a thread the agent opened for itself (a sub-agent, or a turn its report
woke), which is foreground for the model and background for the operator,
and gets its own lane so a burst of them can neither starve the operator nor be starved
by the scheduler.

An unrecognised kind lands in ``background``. That is the conservative direction: a kind
nobody mapped is far more likely to be new autonomous work than a turn the operator is
waiting on, and the one thing this module exists to prevent is unattended work crowding
out attended work.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Literal

type Lane = Literal["interactive", "background", "linked"]

#: Every lane, in the order a readout should list them: most attended first.
LANES: tuple[Lane, ...] = ("interactive", "background", "linked")

#: What an unmapped kind waits in — see the module docstring.
DEFAULT_LANE: Lane = "background"

#: Run kind → lane. The keys are the vocabulary the chat composition path
#: (``routes/chat.py``'s ``compose_turn``) submits under: ``chat`` is the operator's own
#: turn, ``task`` a scheduled task's fire, ``linked`` a thread the agent opened from
#: inside another one. They name the turn's *provenance*, which is exactly what decides
#: the lane — hence one mapping rather than a lane argument at each call site.
LANE_BY_KIND: Mapping[str, Lane] = {
    "chat": "interactive",
    "task": "background",
    "linked": "linked",
    # A thread continuing itself: a sub-agent it launched has reported, and the turn that
    # launched one has long since ended. Foreground for the model and background for the
    # operator, exactly like the ``linked`` threads it shares a lane with — and emphatically
    # not interactive, because nobody is sitting in front of it and a burst of sub-agents
    # finishing at once must not put the operator's own message in a queue.
    "wake": "linked",
    # The operator folding a thread by hand. Interactive because that is literally true —
    # they pressed a button and are watching the summary arrive — and it is not a chat
    # turn: no model drives it, it composes nothing, and it must not drain the
    # queued-message inbox.
    "compaction": "interactive",
}

#: The kinds that are a chat turn — the ones composed by ``compose_turn``, and therefore
#: the ones whose orchestrator drains the queued-message inbox a mid-run send lands in.
#:
#: **Named rather than derived from the lane map**, which is what it used to be. That
#: derivation read as the tidier of the two and was only ever right by coincidence: every
#: kind happened to be a chat turn, so "has a lane" and "is a chat turn" could not be told
#: apart. The first kind that needed a lane *without* being a turn — a hand-started fold —
#: would have been enrolled as one silently, and the symptom is a fold quietly swallowing
#: a message the operator sent while it ran.
CHAT_TURN_KINDS: frozenset[str] = frozenset({"chat", "task", "linked", "wake"})


def lane_for(kind: str) -> Lane:
    """The lane a run of this kind waits in. Never raises — a stored or forwarded kind
    can outlive the code that coined it, and an unknown one has to queue somewhere."""
    return LANE_BY_KIND.get(kind, DEFAULT_LANE)


@dataclass(frozen=True)
class LaneLimits:
    """The host ceiling, and how much of it each lane may take.

    Interactive keeps the number the single pool used to carry and caps at the ceiling
    itself, because a quiet host should hand the operator the whole machine. The
    autonomous lanes are deliberately narrower — their work has no one waiting on it and
    their whole failure mode is volume — and it is that narrowness, not extra slots, that
    keeps the operator out of the queue: see :attr:`attended_floor`.

    ``total`` is the ceiling and defaults to the interactive cap, so the one configured
    number that has always meant "how many runs may this host execute at once" still
    means exactly that after the split.
    """

    interactive: int = 8
    background: int = 2
    linked: int = 3
    total: int | None = None

    @property
    def ceiling(self) -> int:
        """Runs executing at once, across every lane. The only number that bounds the
        host — a lane cap only decides who may hold these slots, never how many exist."""
        return max(1, self.interactive if self.total is None else self.total)

    @property
    def attended_floor(self) -> int:
        """Slots the unattended lanes cannot hold even when both are saturated, and so
        the number of interactive turns that can always start immediately. Derived rather
        than configured: a floor declared separately from the caps that produce it is a
        second source of truth, and it would be the one that was wrong."""
        return max(0, self.ceiling - self.background - self.linked)

    def for_lane(self, lane: Lane) -> int:
        """How many of the ceiling's slots this lane may hold at once."""
        return min(self.ceiling, max(1, int(getattr(self, lane))))


class LaneGate:
    """The semaphores themselves, addressed by run kind.

    Two acquisitions per run, always in this order: the lane's own semaphore, then the
    host ceiling. Lane-first is what makes the caps mean anything — a run that took the
    ceiling first and then blocked at its lane gate would be sitting on a slot it cannot
    use while an interactive turn queued behind it. Taken in one fixed order there is no
    cycle to deadlock on, and the ceiling is only ever held by a run that is executing.

    Holds one semaphore per lane rather than one per kind, so two kinds that share a
    lane share its slots. Constructed outside any event loop (the registry is built at
    app assembly, before the loop is running), which ``asyncio.Semaphore`` has allowed
    since it stopped binding a loop at construction.
    """

    def __init__(self, limits: LaneLimits | None = None) -> None:
        self._limits = limits or LaneLimits()
        self._host = asyncio.Semaphore(self._limits.ceiling)
        self._semaphores: dict[Lane, asyncio.Semaphore] = {
            lane: asyncio.Semaphore(self._limits.for_lane(lane)) for lane in LANES
        }

    @property
    def limits(self) -> LaneLimits:
        return self._limits

    @asynccontextmanager
    async def slot(self, kind: str) -> AsyncIterator[None]:
        """Hold a slot for a run of this kind, for the duration of the block.

        A burst waits at its own lane's gate and nowhere else; what it waits for *after*
        that gate is the one ceiling everything shares. Both permits are released on the
        way out — on a normal return, on an exception, and on cancellation delivered
        while still waiting — because that is what the two nested ``async with``\\ es
        mean, and a permit leaked by a run that failed would narrow the lane for good.
        """
        async with self._semaphores[lane_for(kind)], self._host:
            yield
