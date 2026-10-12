"""The deferred sweep that writes each thread's work summary.

A summary of what the agent did in a conversation is worth writing exactly once the work
has stopped moving. That makes it the opposite shape from auto-titling, which is settled
inside the first turn: there is no moment during a turn at which "what this thread did" is
a finished question, so this hangs off a timer instead and asks it of threads that have
been quiet for a while (:func:`services.settings_store.get_work_summary_idle_minutes`, the
operator's own dial).

Three things the sweep refuses to do, each of which would be a real defect rather than
merely wasted work:

- **It does not run while the vault is locked.** The history it would read and the summary
  it would write are both sealed, so there is no key and nothing to do — the pass returns
  immediately rather than raising its way through a decrypt, the same posture
  :class:`services.scheduler.SchedulerService` takes for the same reason.
- **It does not summarize a thread with a live run.** The registry is asked before each
  candidate, because "idle for fifteen minutes" is measured off ``updated_at``, which a
  run that has not persisted its turn yet has not moved. A summary written under a
  streaming answer would describe the thread as it was before the turn it is watching.
- **It does not empty the queue in one pass.** A workspace full of threads that predate the
  feature is a backfill, not an emergency: the candidate query is capped
  (``work_summary_max_per_sweep``) so the first sweep after an upgrade spreads its model
  calls over several minutes rather than firing one per thread at the background endpoint
  at once. Ordering by most-recently-active means the threads the operator might actually
  re-enter are the ones that get done first.

Failure is per-thread and never fatal: :func:`agent.work_summary.summarize_work` is
best-effort and returns ``None``, and anything raised around one candidate is logged and
the rest of the pass continues. The loop itself is a :class:`harness.PeriodicTask`, so a
pass that fails outright leaves the loop up.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Protocol

from pydantic_ai import ModelMessage
from pydantic_ai.models import Model
from pydantic_ai.settings import ModelSettings

from core.config import Settings
from core.periodic import PeriodicTask
from core.vault import Vault
from runs import RunRegistry
from services.conversations import ConversationStore
from services.registry import ModelRegistry
from services.settings_store import SettingsStore, get_work_summary_idle_minutes

logger = logging.getLogger(__name__)


class SummarizeWork(Protocol):
    """What this sweep needs a summarizer to be — `agent.work_summary.summarize_work`.

    Named as a shape and injected, rather than imported, because ``agent`` sits *above*
    ``services``: a module-level import here would invert the layering, and a
    function-level one would hide that it had. The assembly layer owns both sides and is
    the honest place to introduce them, which is also what lets a test drive a whole pass
    with a two-line stub and no model at all."""

    async def __call__(
        self,
        model: Model,
        history: list[ModelMessage],
        *,
        reasoning_off: ModelSettings | None = None,
        timeout_s: float | None = None,
        max_tokens: int | None = None,
    ) -> str | None: ...


class WorkSummaryService:
    """Owns the sweep loop and one pass of it.

    Everything it needs is injected rather than resolved from app state, so a test drives a
    real pass over a real store with a scripted model and no application at all.
    """

    def __init__(
        self,
        *,
        store: ConversationStore,
        settings_store: SettingsStore,
        models: ModelRegistry,
        runs: RunRegistry,
        vault: Vault,
        settings: Settings,
        owner_id: str,
        summarize: SummarizeWork,
    ) -> None:
        self._store = store
        self._settings_store = settings_store
        self._models = models
        self._runs = runs
        self._vault = vault
        self._settings = settings
        self._owner_id = owner_id
        self._summarize_work = summarize
        self._task = PeriodicTask(
            "work-summaries",
            interval_s=settings.work_summary_sweep_interval_s,
            work=self.sweep,
            logger=logger,
            # A failed sweep loses the thing it existed to produce, unlike a reaper's, which
            # simply tries the same sessions again next minute. Worth saying out loud.
            log_level=logging.WARNING,
        )

    async def start(self) -> None:
        await self._task.start()

    async def stop(self) -> None:
        await self._task.stop()

    async def sweep(self) -> int:
        """One pass. Returns how many summaries were written — for the tests and for a
        caller that wants to drive a pass by hand, never for a decision."""
        if not self._vault.unlocked_event.is_set():
            # Nothing to decrypt the history with and nothing to seal the summary under.
            # Not an error and not worth a log line every interval: the app simply has not
            # been unlocked yet, and the next pass after it is will find the same threads.
            return 0
        idle_minutes = await get_work_summary_idle_minutes(self._settings_store, self._owner_id)
        idle_before = datetime.now(UTC) - timedelta(minutes=idle_minutes)
        candidates = await self._store.work_summary_candidates(
            self._owner_id,
            idle_before=idle_before,
            limit=self._settings.work_summary_max_per_sweep,
        )
        written = 0
        for conversation_id in candidates:
            if self._runs.active_run_for(conversation_id, self._owner_id) is not None:
                # Idle is measured off `updated_at`, which an in-flight turn has not moved
                # yet — so a thread can look quiet while an answer is streaming into it.
                continue
            try:
                if await self._summarize(conversation_id):
                    written += 1
            except Exception:  # noqa: BLE001 — one bad thread must not end the pass
                logger.warning(
                    "work summary: conversation %s failed", conversation_id, exc_info=True
                )
        return written

    async def _summarize(self, conversation_id: str) -> bool:
        """Summarize one thread and store the result; False when there was nothing to store.

        The model is resolved per thread rather than once per pass because
        ``resolve_background`` is the same utility→main rule titling uses and the operator
        can rebind a role between two candidates; resolving it once would pin a whole
        backfill to whatever was bound when it started."""
        history = await self._store.history(conversation_id)
        if not history:
            return False
        resolved = await self._models.resolve_background(owner_id=self._owner_id)
        summary = await self._summarize_work(
            resolved.model,
            history,
            reasoning_off=resolved.reasoning_off,
            timeout_s=self._settings.work_summary_timeout_s,
            max_tokens=self._settings.work_summary_max_tokens,
        )
        if not summary:
            return False
        await self._store.set_work_summary(conversation_id, summary)
        return True
