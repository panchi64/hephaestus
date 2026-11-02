"""Where finished spans go: the trace store, and the live stream of the run they belong to."""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol

from runs.run import Run

from .collector import SpanRecord

logger = logging.getLogger(__name__)


class SpanStore(Protocol):
    def add(self, record: SpanRecord) -> None: ...


class SpanSink:
    """Receives records from any thread and lands them on the event loop it was built on.

    The store is attached only while the workspace is open; records arriving while it is
    locked have no database to go to and are dropped."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop: asyncio.AbstractEventLoop = loop
        self._store: SpanStore | None = None

    def attach(self, store: SpanStore | None) -> None:
        self._store = store

    def deliver(self, record: SpanRecord, run: Run | None) -> None:
        if self._loop.is_closed():
            return
        _ = self._loop.call_soon_threadsafe(self._land, record, run)

    def _land(self, record: SpanRecord, run: Run | None) -> None:
        if self._store is not None:
            self._store.add(record)
        # A run's own span ends after its stream may have closed.
        if run is not None and not run.stream.closed:
            try:
                _ = run.emit(record.event())
            except RuntimeError:
                logger.debug("span for run %s arrived after its stream closed", run.id)
