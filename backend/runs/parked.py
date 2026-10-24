"""Where a run parked on the operator writes itself, so a restart keeps the park.

The registry calls the journal; what is written is the orchestrator layer's business, so
this module knows only that a run is parked and when it stops being.
"""

from __future__ import annotations

from typing import Protocol, override

from .run import Run


class ParkedJournal(Protocol):
    """Fire-and-forget: an implementation queues the write and never blocks the caller."""

    def record(self, run: Run) -> bool:
        """Write the parked run's continuation. Returns whether a write was queued, so a
        run whose continuation cannot be written is not left to a restart that cannot
        restore it."""
        ...

    def advance(self, run_id: str, last_seq: int) -> None:
        """The parked run's stream reached ``last_seq``: keep the stored seq from trailing
        it, without rewriting the continuation."""
        ...

    def forget(self, run_id: str) -> None:
        """Drop it: the run resumed, was cancelled or ended."""
        ...


class _NoParkedJournal(ParkedJournal):
    @override
    def advance(self, run_id: str, last_seq: int) -> None:
        pass

    @override
    def record(self, run: Run) -> bool:
        return False

    @override
    def forget(self, run_id: str) -> None:
        pass


NO_PARKED_JOURNAL: ParkedJournal = _NoParkedJournal()
