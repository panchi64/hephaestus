"""Where a run parked on the operator writes itself, so a restart keeps the park.

The registry calls the journal; what is written is the orchestrator layer's business, so
this module knows only that a run is parked and when it stops being.
"""

from __future__ import annotations

from typing import Protocol, override

from .run import Run


class ParkedJournal(Protocol):
    """Fire-and-forget: an implementation queues the write and never blocks the caller."""

    def record(self, run: Run) -> None:
        """Write the parked run's continuation."""
        ...

    def forget(self, run_id: str) -> None:
        """Drop it: the run resumed, was cancelled or ended."""
        ...


class _NoParkedJournal(ParkedJournal):
    @override
    def record(self, run: Run) -> None:
        pass

    @override
    def forget(self, run_id: str) -> None:
        pass


NO_PARKED_JOURNAL: ParkedJournal = _NoParkedJournal()
