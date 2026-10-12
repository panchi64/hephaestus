"""Offline mode — the connectivity state machine + the web-tool gate + the routes.

The state machine is driven with stub containers (start/stop counters) and an injected
probe, so no test touches the network. `_observe` folds one connectivity sample, exactly
as the monitor loop does, letting us exercise the hysteresis without real timers.
"""

from __future__ import annotations

from core.db import init_db, make_engine
from services.offline import OfflineModeService
from services.settings_store import OFFLINE_AUTO_KEY, OFFLINE_MANUAL_KEY, SettingsStore
from tests._helpers import client_app

OWNER = "operator"
WEB_TOOLS = frozenset({"web_search", "web_fetch"})


class _StubContainer:
    """A managed container stand-in: counts start/stop and tracks running state. Both
    are idempotent like the real ones, so a boot-time teardown of a never-started
    container is harmless."""

    def __init__(self) -> None:
        self.starts = 0
        self.stops = 0
        self.running = False

    async def start(self) -> None:
        self.starts += 1
        self.running = True

    async def stop(self) -> None:
        self.stops += 1
        self.running = False


async def _make(
    *,
    online: bool = True,
    manual: bool | None = None,
    auto: bool | None = None,
    fail_threshold: int = 3,
    recover_threshold: int = 2,
    network_tools: frozenset[str] = WEB_TOOLS,
) -> tuple[OfflineModeService, _StubContainer, _StubContainer]:
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    store = SettingsStore(engine)
    if manual is not None:
        await store.set(OWNER, OFFLINE_MANUAL_KEY, "true" if manual else "false")
    if auto is not None:
        await store.set(OWNER, OFFLINE_AUTO_KEY, "true" if auto else "false")
    searxng, browser = _StubContainer(), _StubContainer()

    async def probe() -> bool:
        return online

    svc = OfflineModeService(
        searxng=searxng,
        browser=browser,
        settings_store=store,
        owner_id=OWNER,
        anchors=["unused"],
        interval_s=3600,  # the monitor won't tick during a test; transitions via _observe
        timeout_s=1,
        fail_threshold=fail_threshold,
        recover_threshold=recover_threshold,
        probe=probe,
        network_tools=network_tools,
    )
    return svc, searxng, browser


async def test_boot_online_starts_both_containers():
    svc, searxng, browser = await _make(online=True)
    await svc.start()
    try:
        assert searxng.running and browser.running
        assert searxng.starts == 1 and browser.starts == 1
        assert svc.state().online is True
        assert svc.state().effective_offline is False
        assert svc.web_tools_disabled() == frozenset()
    finally:
        await svc.stop()


async def test_boot_offline_never_starts_the_browser():
    svc, searxng, browser = await _make(online=False)
    await svc.start()
    try:
        # Probe-first / fail-closed: nothing is brought up on an offline boot.
        assert searxng.starts == 0 and browser.starts == 0
        assert not searxng.running and not browser.running
        assert svc.state().online is False
        assert svc.state().effective_offline is True
        assert svc.web_tools_disabled() == WEB_TOOLS
    finally:
        await svc.stop()


async def test_the_offline_gate_withholds_whatever_the_features_declared():
    # Which tools need the internet is the declaring feature's knowledge, arriving here as
    # the union of every manifest's `network_tools`. Offline mode suspends what it was
    # given — so a feature that grows another network tool is covered the day it lands,
    # with nothing to remember to edit here.
    declared = frozenset({"web_search", "web_fetch", "weather_forecast"})
    svc, _searxng, _browser = await _make(online=False, network_tools=declared)
    await svc.start()
    try:
        assert svc.web_tools_disabled() == declared
    finally:
        await svc.stop()


async def test_a_build_that_declared_nothing_withholds_nothing():
    # The gate is not allowed to invent a set of its own: no declarations, no withholding,
    # even fully offline.
    svc, _searxng, _browser = await _make(online=False, network_tools=frozenset())
    await svc.start()
    try:
        assert svc.state().effective_offline is True
        assert svc.web_tools_disabled() == frozenset()
    finally:
        await svc.stop()


async def test_fail_threshold_suspends_then_recovery_resumes():
    svc, searxng, browser = await _make(online=True, fail_threshold=3, recover_threshold=2)
    await svc.start()
    try:
        # Two failures stay under the threshold — no flapping.
        await svc._observe(False)
        await svc._observe(False)
        assert searxng.running and svc.state().online is True
        # The third crosses it → offline, containers torn down, web tools hidden.
        await svc._observe(False)
        assert not searxng.running and not browser.running
        assert svc.state().online is False and svc.state().effective_offline is True
        assert svc.web_tools_disabled() == WEB_TOOLS
        # One success is under the recover threshold (2) — still offline.
        await svc._observe(True)
        assert not searxng.running and svc.state().effective_offline is True
        # The second recovers → online, containers resumed.
        await svc._observe(True)
        assert searxng.running and browser.running
        assert searxng.starts == 2  # boot + resume
        assert svc.state().online is True and svc.state().effective_offline is False
        assert svc.web_tools_disabled() == frozenset()
    finally:
        await svc.stop()


async def test_manual_offline_survives_a_connectivity_recovery():
    svc, searxng, browser = await _make(online=True)
    await svc.start()
    try:
        await svc.set_manual(True)
        assert not searxng.running and svc.state().effective_offline is True
        # Connectivity is fine throughout; successes must not undo a manual offline.
        await svc._observe(True)
        await svc._observe(True)
        assert not searxng.running and svc.state().effective_offline is True
        # Clearing the manual switch comes back online (connectivity is up).
        await svc.set_manual(False)
        assert searxng.running and svc.state().effective_offline is False
    finally:
        await svc.stop()


async def test_manual_setting_persists_across_a_restart():
    # A persisted manual=true is honoured at boot even when connectivity is fine —
    # and the browser is never started.
    svc, searxng, browser = await _make(online=True, manual=True)
    await svc.start()
    try:
        assert svc.state().manual is True
        assert svc.state().effective_offline is True
        assert searxng.starts == 0 and browser.starts == 0
    finally:
        await svc.stop()


async def test_auto_off_ignores_connectivity_loss():
    svc, searxng, browser = await _make(online=True, auto=False, fail_threshold=2)
    await svc.start()
    try:
        assert searxng.running  # auto off + online → containers up at boot
        # Lose connectivity past the threshold: `online` tracks it, but with auto off the
        # effective state (and the containers) don't change.
        await svc._observe(False)
        await svc._observe(False)
        await svc._observe(False)
        assert svc.state().online is False
        assert svc.state().effective_offline is False
        assert searxng.running and browser.running
        assert svc.web_tools_disabled() == frozenset()
    finally:
        await svc.stop()


async def test_routes_read_and_toggle_manual_offline():
    async with client_app() as (client, app):
        # Boot assumes online in tests (no probing), so nothing is paused.
        body = (await client.get("/offline")).json()
        assert body == {
            "manual_offline": False,
            "auto_detect": True,
            "online": True,
            "effective_offline": False,
        }

        # Force offline → effective flips and the overview web rows reflect it.
        put = (await client.put("/offline", json={"manual_offline": True})).json()
        assert put["manual_offline"] is True and put["effective_offline"] is True

        overview = (await client.get("/overview")).json()
        rows = {c["key"]: c for c in overview["capabilities"]}
        assert rows["web_search"]["detail"] == "offline mode — paused"
        assert rows["web_fetch"]["detail"] == "offline mode — paused"
        assert rows["web_search"]["status"] == "warn"

        # Clear it and turn auto-detect off in one place; state reads back.
        cleared = (
            await client.put("/offline", json={"manual_offline": False, "auto_detect": False})
        ).json()
        assert cleared["effective_offline"] is False
        assert cleared["auto_detect"] is False


async def test_searxng_stop_then_start_is_restartable():
    """The offline monitor toggles the managed SearXNG up and down, so stop() must reset
    it for a fresh start() (and clear the stale base_url that search reads)."""
    from services.searxng import ManagedSearxng

    sx = ManagedSearxng(
        enabled=True,
        vm=None,
        data_dir=make_tmp_dir(),
        startup_timeout_s=1.0,
        external_base_url="http://searx.example:8888",
    )
    # An external instance needs no VM service — start() just adopts the URL.
    await sx.start()
    assert sx.base_url == "http://searx.example:8888"
    await sx.stop()
    assert sx.base_url is None  # search must read this as unavailable while offline
    await sx.start()
    assert sx.base_url == "http://searx.example:8888"  # restartable
    await sx.stop()


def make_tmp_dir():
    import tempfile
    from pathlib import Path

    return Path(tempfile.mkdtemp())
