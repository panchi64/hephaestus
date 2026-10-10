"""The context meter a cold read reports — the reload twin of the live gauge frame.

A live turn measures the context in ``agent/metrics.py``: the footprint of the history the
next request replays, read off responses that landed *since the newest fold*, with the
estimate standing in until one does. A reload has no run to take that from, so it rebuilds
the same figure from the stored thread — and has to rebuild it **the same way**, or a
reopened thread shows a different gauge than the one the operator watched settle. That
means measuring :meth:`ConversationStore.replay_history` (the summary plus the turns it
doesn't cover), never the whole transcript, and honouring the fold boundary it reports.

One function, because the read endpoint and every navigation endpoint that returns the
post-move thread (version switch, rewind, fork) assemble the detail through one helper and
must agree. Nothing here decides anything the stream doesn't: it is the same arithmetic
(``footprint_or_estimate``, ``compose``, ``ContextWindow.from_used``) over stored state.
"""

from __future__ import annotations

from dataclasses import dataclass

from core.config import get_settings
from runs import ContextWindow, FoldPoint
from services.context_budget import compose
from services.conversations import ConversationStore, footprint_or_estimate
from services.registry import ModelRegistry
from services.settings_store import (
    SettingsStore,
    get_auto_compact,
    get_context_thresholds,
    resolve_compaction_enabled,
)


@dataclass(frozen=True)
class ContextMeter:
    """What a cold read knows about how full the thread is.

    ``used`` is the footprint (``None`` only for a thread with no messages), ``window`` the
    model's ceiling when it was resolved, and ``context`` the gauge state derived from the
    two — ``None`` when there is nothing to measure or no ceiling to measure it against."""

    used: int | None
    window: int | None
    context: ContextWindow | None


async def read_context_meter(
    conversation_id: str,
    *,
    owner_id: str,
    store: ConversationStore,
    models: ModelRegistry,
    settings_store: SettingsStore,
) -> ContextMeter:
    """Seed the context meter from the thread as the next request would carry it.

    The footprint is the provider's own report where one landed after the newest fold, and
    otherwise the estimate the live gauge falls back to — so reopening a thread neither
    blanks a ring that was filling a moment ago nor shows the pre-fold figure for a thread
    that has been folded and not answered since. The window is only paid for when there is
    something to measure against it, and is the default ``main`` model's (no
    per-conversation endpoint is persisted, so that is what the next turn would run on)."""
    replay = await store.replay_history(conversation_id)
    overhead = await store.get_overhead(conversation_id)
    used = footprint_or_estimate(
        replay.messages,
        overhead,
        fallback_overhead_tokens=get_settings().context_overhead_fallback_tokens,
        reported_from=replay.messages[replay.boundary :],
    )
    if used is None:
        return ContextMeter(used=None, window=None, context=None)
    window = await models.main_context_window(owner_id)
    # The fold mark, resolved the way a turn would resolve it — the operator's stored
    # threshold with *this thread's* on/off override folded in. Read here rather than left
    # to the client because the client only knows the global setting, and a thread whose
    # folding the operator switched off would otherwise show an armed mark that nothing is
    # going to act on.
    auto = await get_auto_compact(settings_store, owner_id)
    override = await store.get_compaction_override(conversation_id)
    context = ContextWindow.from_used(
        used,
        window,
        # The operator's own boundaries, not the defaults: a reloaded thread must show the
        # gauge in the colour the live turn left it, and reading the stored pair here is
        # what keeps a cold load from quietly re-deriving severity against 75/90.
        await get_context_thresholds(settings_store, owner_id),
        # A reload has no request to measure — neither the brief nor the tool schemas reach
        # the message history — so the split leans on what this thread's last turn
        # recorded. Same turn as `used` above, so both halves of the readout describe the
        # same request. Absent for a thread that hasn't run one since this was recorded,
        # which shows as no breakdown rather than a guessed one.
        compose(used, overhead, replay.messages),
        FoldPoint.of(auto.threshold, active=resolve_compaction_enabled(override, auto.enabled)),
    )
    return ContextMeter(used=used, window=window, context=context)
