"""Compaction defaults and the operator's dials over them.

Three things are worth pinning here, because each of them silently changes when a fold
happens:

- **The config defaults.** 80% (not 95%, which folds with no room left for the turn that
  triggered the fold), a summarizer input ceiling of 32k, and a summarizer timeout *below*
  the inactivity watchdog — a summarizer allowed to run as long as the watchdog would let
  the watchdog kill the run it was trying to save.
- **The stored preferences**, and their resolution into the policy the engine reads.
- **The round-trip through ``PUT /chat/settings``**, including the rule that a body
  touching one field of the group leaves the others alone.
"""

from __future__ import annotations

import pytest

from agent.summarize import resolve_auto_compact_policy
from core.config import Settings
from core.db import init_db, make_engine
from services.settings_store import (
    AUTO_COMPACT_ENABLED_KEY,
    AUTO_COMPACT_THRESHOLD_KEY,
    AutoCompactSettings,
    SettingsStore,
    get_auto_compact,
    set_auto_compact,
)

from ._helpers import client_app
from ._json import json_body, n

OWNER = "op"


def _store() -> SettingsStore:
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    return SettingsStore(engine)


# --- config defaults ---------------------------------------------------------


def test_the_compaction_defaults():
    cfg = Settings()
    assert cfg.auto_compact_enabled is True
    assert cfg.auto_compact_threshold == pytest.approx(0.80)


def test_the_summary_has_no_time_or_length_setting():
    """The summary replaces the thread, so nothing may cut it short — not a deadline and
    not an output budget. A setting for either would be a limit waiting to be set."""
    fields = Settings.model_fields
    assert "auto_compact_timeout_s" not in fields
    assert "auto_compact_max_tokens" not in fields


def test_there_is_no_summarizer_input_budget():
    """The summary is written by the thread's own model over the replay it already serves,
    so there is no second model's window to chunk a transcript for."""
    assert "auto_compact_input_max_tokens" not in Settings.model_fields


def test_the_overhead_fallback_is_not_zero():
    """A thread whose turns predate the per-thread overhead record must not be told it has
    a free 14k of instructions and tool schemas."""
    assert Settings().context_overhead_fallback_tokens > 0


def test_the_anthropic_cache_ttl_default():
    assert Settings().anthropic_cache_ttl == "5m"


# --- the store ---------------------------------------------------------------


async def test_the_group_round_trips_through_the_store():
    store = _store()
    stored = await set_auto_compact(store, OWNER, AutoCompactSettings(enabled=False, threshold=0.7))
    assert (stored.enabled, stored.threshold) == (False, 0.7)
    read = await get_auto_compact(store, OWNER)
    assert (read.enabled, read.threshold) == (False, 0.7)


# --- policy resolution -------------------------------------------------------


async def test_the_resolved_policy_carries_the_stored_threshold():
    """The engine reads the policy, never the store — so the operator's dial has to arrive
    through this resolution or it does nothing at all."""
    store = _store()
    await store.set(OWNER, AUTO_COMPACT_THRESHOLD_KEY, "0.6")
    policy = await resolve_auto_compact_policy(store, OWNER)
    assert policy.threshold == pytest.approx(0.6)


async def test_the_per_conversation_override_still_wins_on_enablement():
    store = _store()
    await store.set(OWNER, AUTO_COMPACT_ENABLED_KEY, "true")
    policy = await resolve_auto_compact_policy(store, OWNER, override=False)
    assert policy.enabled is False


# --- the route ---------------------------------------------------------------


async def test_a_put_touching_only_the_threshold_leaves_the_group_alone():
    async with client_app() as (client, _app):
        _ = await client.put("/chat/settings", json={"auto_compact_enabled": False})
        body = json_body(
            await client.put("/chat/settings", json={"auto_compact_threshold": 0.5})
        )
        assert n(body["auto_compact_threshold"]) == pytest.approx(0.5)
        assert body["auto_compact_enabled"] is False
