"""Owner-scoped settings store — the getters' corruption-safety + the batched read."""

from __future__ import annotations

from core.config import get_settings
from core.db import init_db, make_engine
from services.settings_store import (
    AUTO_COMPACT_ENABLED_KEY,
    AUTO_COMPACT_THRESHOLD_KEY,
    INACTIVITY_TIMEOUT_KEY,
    SUBAGENT_LIMIT_KEY,
    WALL_CLOCK_TIMEOUT_KEY,
    SettingsStore,
    get_auto_compact,
    get_inactivity_timeout,
    get_subagent_limit,
    get_wall_clock_timeout,
    resolve_compaction_enabled,
    set_inactivity_timeout,
    set_subagent_limit,
    set_wall_clock_timeout,
)

OWNER = "op"


def _store() -> SettingsStore:
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    return SettingsStore(engine)


async def test_get_many_reads_several_keys_in_one_call():
    store = _store()
    await store.set(OWNER, AUTO_COMPACT_THRESHOLD_KEY, "0.5")

    values = await store.get_many(OWNER, (AUTO_COMPACT_ENABLED_KEY, AUTO_COMPACT_THRESHOLD_KEY))
    assert values == {AUTO_COMPACT_THRESHOLD_KEY: "0.5"}
    # An absent key is simply omitted — the caller applies its own default.
    assert AUTO_COMPACT_ENABLED_KEY not in values


async def test_get_many_ignores_keys_it_was_not_asked_for():
    # Retiring a setting needs no migration: only the keys a getter names are read, so a
    # row left behind by a removed preference is inert rather than a stale override.
    store = _store()
    await store.set(OWNER, "chat.compaction_enabled", "false")  # a setting that no longer exists
    await store.set(OWNER, AUTO_COMPACT_THRESHOLD_KEY, "0.5")

    values = await store.get_many(OWNER, (AUTO_COMPACT_ENABLED_KEY, AUTO_COMPACT_THRESHOLD_KEY))
    assert values == {AUTO_COMPACT_THRESHOLD_KEY: "0.5"}
    auto = await get_auto_compact(store, OWNER)
    assert auto.enabled == get_settings().auto_compact_enabled


async def test_get_auto_compact_uses_config_defaults_when_unset():
    store = _store()
    cfg = get_settings()
    auto = await get_auto_compact(store, OWNER)
    assert auto.enabled == cfg.auto_compact_enabled
    assert auto.threshold == cfg.auto_compact_threshold


async def test_get_auto_compact_round_trips_overrides():
    store = _store()
    await store.set(OWNER, AUTO_COMPACT_ENABLED_KEY, "false")
    await store.set(OWNER, AUTO_COMPACT_THRESHOLD_KEY, "0.75")

    auto = await get_auto_compact(store, OWNER)
    assert auto.enabled is False and auto.threshold == 0.75


async def test_corrupted_enabled_flag_falls_back_to_the_config_default():
    # A stored value that is neither "true" nor "false" (legacy/corrupt/case-variant) must fall
    # back to the config default — not silently read as False the way a bare `== "true"` would.
    store = _store()
    await store.set(OWNER, AUTO_COMPACT_ENABLED_KEY, "True")  # wrong case
    auto = await get_auto_compact(store, OWNER)
    assert auto.enabled == get_settings().auto_compact_enabled


async def test_corrupted_threshold_falls_back_to_the_config_default():
    # The threshold is a fraction in (0, 1]: 0 would fire compaction on an empty thread and
    # anything above 1 could never fire at all, so both fall back rather than thrash.
    store = _store()
    cfg = get_settings()
    for bad in ("not-a-number", "0", "-0.5", "1.5"):
        await store.set(OWNER, AUTO_COMPACT_THRESHOLD_KEY, bad)
        assert (await get_auto_compact(store, OWNER)).threshold == cfg.auto_compact_threshold


def test_resolve_compaction_enabled_precedence():
    # null override ⇒ inherit the global; a concrete override wins either way.
    assert resolve_compaction_enabled(None, True) is True
    assert resolve_compaction_enabled(None, False) is False
    assert resolve_compaction_enabled(False, True) is False
    assert resolve_compaction_enabled(True, False) is True


async def test_there_is_no_subagent_cap_until_the_operator_sets_one():
    store = _store()
    # Not a fallback from a config default — there is no config default, because "however
    # many the work splits into" is the intended behaviour rather than an unset one.
    assert await get_subagent_limit(store, OWNER) is None

    _ = await set_subagent_limit(store, OWNER, 3)
    assert await get_subagent_limit(store, OWNER) == 3

    _ = await set_subagent_limit(store, OWNER, None)
    assert await get_subagent_limit(store, OWNER) is None


async def test_a_corrupted_subagent_cap_reads_as_no_cap():
    store = _store()
    for bad in ("0", "-5", "not-a-number"):
        await store.set(OWNER, SUBAGENT_LIMIT_KEY, bad)
        # Back to how the product behaves out of the box, rather than to a restriction the
        # operator never asked for — the same direction every other getter here falls.
        assert await get_subagent_limit(store, OWNER) is None, bad


async def test_get_inactivity_timeout_uses_config_default_when_unset():
    store = _store()
    assert await get_inactivity_timeout(store, OWNER) == get_settings().run_inactivity_timeout_s


async def test_inactivity_timeout_round_trips_an_override():
    store = _store()
    stored = await set_inactivity_timeout(store, OWNER, 300.0)
    assert stored == 300.0
    assert await get_inactivity_timeout(store, OWNER) == 300.0


async def test_corrupted_inactivity_timeout_falls_back_to_the_config_default():
    # A stored 0, negative, or non-numeric value must fall back to the config default
    # rather than silently disabling the watchdog (0) or reading as a nonsense bound.
    store = _store()
    cfg = get_settings()
    await store.set(OWNER, INACTIVITY_TIMEOUT_KEY, "0")
    assert await get_inactivity_timeout(store, OWNER) == cfg.run_inactivity_timeout_s
    await store.set(OWNER, INACTIVITY_TIMEOUT_KEY, "-5")
    assert await get_inactivity_timeout(store, OWNER) == cfg.run_inactivity_timeout_s
    await store.set(OWNER, INACTIVITY_TIMEOUT_KEY, "not-a-number")
    assert await get_inactivity_timeout(store, OWNER) == cfg.run_inactivity_timeout_s


async def test_get_wall_clock_timeout_uses_config_default_when_unset():
    # Which is None out of the box — a run is bounded by its request limit, not a clock.
    store = _store()
    assert await get_wall_clock_timeout(store, OWNER) == get_settings().run_wall_clock_timeout_s


async def test_wall_clock_timeout_round_trips_an_override():
    store = _store()
    stored = await set_wall_clock_timeout(store, OWNER, 3600.0)
    assert stored == 3600.0
    assert await get_wall_clock_timeout(store, OWNER) == 3600.0


async def test_wall_clock_timeout_off_is_stored_rather_than_falling_back():
    # The one getter where "no value" is a real answer: an operator who switches the bound
    # off must stay off even against a deploy that sets `run_wall_clock_timeout_s`.
    store = _store()
    _ = await set_wall_clock_timeout(store, OWNER, 3600.0)
    assert await set_wall_clock_timeout(store, OWNER, None) is None
    assert await get_wall_clock_timeout(store, OWNER) is None
    assert await store.get(OWNER, WALL_CLOCK_TIMEOUT_KEY) == ""


async def test_corrupted_wall_clock_timeout_falls_back_to_the_config_default():
    # 0, negative, and non-numeric are corruption, not the operator saying "off" — that is
    # the empty value above, and only that one.
    store = _store()
    cfg = get_settings()
    for bad in ("0", "-5", "not-a-number"):
        await store.set(OWNER, WALL_CLOCK_TIMEOUT_KEY, bad)
        assert await get_wall_clock_timeout(store, OWNER) == cfg.run_wall_clock_timeout_s
