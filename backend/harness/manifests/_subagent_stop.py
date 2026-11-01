"""Stopping spawned agents: the cascade below a thread and the marks a launch in flight reads.

Private (the underscore keeps manifest discovery from taking it for a feature).
"""

from __future__ import annotations

import time
from collections import OrderedDict
from typing import override

from models.subagent import SubagentRecord
from runs import RunRegistry
from runs.inbox import StopMark
from runs.run import StopCause
from services.subagent_store import LIVE_STATUSES, SubagentStore
from services.subagents import SubtreeStopper

from ._subagent_wake import SubagentWake

#: How many thread stops are remembered.
_STOP_MARKS_KEPT = 256


class SubagentStopper(SubtreeStopper):
    def __init__(self, records: SubagentStore, wake: SubagentWake, runs: RunRegistry) -> None:
        self._records: SubagentStore = records
        self._wake: SubagentWake = wake
        self._runs: RunRegistry = runs
        #: When each thread was last stopped, by monotonic clock, for launches in flight.
        self._marks: OrderedDict[str, tuple[float, StopCause]] = OrderedDict()

    @override
    async def stop_below(self, owner_id: str, conversation_id: str) -> list[str]:
        """Stop every live agent below a thread, for the operator."""
        self._mark(conversation_id, StopCause.operator)
        below = await self._records.descendants(conversation_id, owner_id)
        rows = [
            row
            for row in below
            if row.status in LIVE_STATUSES
            or self._runs.active_run_for(row.child_conversation_id, owner_id) is not None
        ]
        # A finished agent isn't stopped, so a report from below would wake it; its inbox
        # waits for the operator instead, unless the Stop turns out to stop nothing.
        stopping = {row.id for row in rows}
        held: list[tuple[str, StopMark]] = []
        for row in below:
            if row.id not in stopping:
                inbox = self._runs.inbox(row.child_conversation_id, owner_id)
                held.append((row.child_conversation_id, inbox.stop()))
        try:
            stopped = await self.stop_rows(owner_id, rows, StopCause.operator)
        except BaseException:
            self._release(owner_id, held)
            raise
        if not stopped:
            self._release(owner_id, held)
        return stopped

    async def stop_rows(
        self, owner_id: str, rows: list[SubagentRecord], cause: StopCause
    ) -> list[str]:
        """Cancel each row and the run on its thread, returning the handles stopped."""
        # Marked before any cancel so the resulting reports don't wake an agent being stopped.
        for each in rows:
            self._mark(each.child_conversation_id, cause)
            await self._records.settle(each.id, status="cancelled")
            self._wake.note_stop(each.child_conversation_id, cause)
        stopped: list[str] = []
        for each in rows:
            run = self._runs.active_run_for(each.child_conversation_id, owner_id)
            if run is None or run.is_terminal:
                continue
            if run.stop_cause is None:
                run.stop_cause = cause
            if await self._runs.cancel(run.id):
                stopped.append(each.handle or each.spec_name)
        return stopped

    def stopped_since(self, conversation_id: str, since: float) -> StopCause | None:
        """Who stopped this thread after ``since``, which a launch in flight would have missed."""
        marked = self._marks.get(conversation_id)
        return marked[1] if marked is not None and marked[0] >= since else None

    def _release(self, owner_id: str, held: list[tuple[str, StopMark]]) -> None:
        for conversation_id, mark in held:
            self._runs.inbox(conversation_id, owner_id).undo_stop(mark)

    def _mark(self, conversation_id: str, cause: StopCause) -> None:
        self._marks[conversation_id] = (time.monotonic(), cause)
        self._marks.move_to_end(conversation_id)
        while len(self._marks) > _STOP_MARKS_KEPT:
            _ = self._marks.popitem(last=False)
