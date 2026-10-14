"""Offline mode — suspend the web services when there's no internet to use them.

The agent's two web capabilities each run a long-lived service in the platform VM: the
managed SearXNG (:class:`services.searxng.ManagedSearxng`) and the headless browser
(:class:`services.webfetch.browser.ManagedBrowser`). With no connectivity they
burn RAM/CPU producing nothing, so this service watches the link and **stops them
when offline**, bringing them **back automatically when connectivity returns**.

Four facts, one verdict:

- ``manual`` (persisted) — the operator forced offline.
- ``auto`` (persisted) — the auto-detect master switch (default on).
- ``online`` (runtime) — the monitor's debounced connectivity verdict.
- ``effective_offline = manual OR (auto AND NOT online)`` — what actually drives the
  two services and the web-tool gate.

**Boot is probe-first / fail-closed:** :meth:`start` runs one connectivity check and sets
the initial ``online`` *before* deciding whether to launch the services — so a host that
boots with no internet never spins up the heavy browser at all. Thereafter the monitor
applies hysteresis (``fail_threshold`` consecutive failures to declare offline,
``recover_threshold`` successes to declare online) so the services don't flap on a flaky
link. Manual offline survives a connectivity recovery — we never override the operator's
explicit choice.

The web capabilities already degrade gracefully when their service is down (search
raises ``DegradedCapabilityError``, fetch checks ``browser.available``), so tearing them
down is safe; on top of that the offline window hides the ``web_search``/``web_fetch``
tools from the agent via the standard enabled-gate (:func:`web_tools_disabled`).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from core.connectivity import check_online
from core.tasks import STOP_TIMEOUT_S, cancel_and_wait
from services.settings_store import OFFLINE_AUTO_KEY, OFFLINE_MANUAL_KEY, SettingsStore

logger = logging.getLogger(__name__)


def _flag(value: bool) -> str:
    """The on-disk form of a switch — settings are plain strings."""
    return "true" if value else "false"


class _GatedService(Protocol):
    """The slice of a managed service the offline monitor drives — both
    :class:`ManagedSearxng` and :class:`ManagedBrowser` satisfy it, and tests pass a
    stub. ``start``/``stop`` are idempotent (each is a no-op when already in that
    state), so a teardown at boot-while-never-started is harmless."""

    async def start(self) -> None: ...
    async def stop(self) -> None: ...


@dataclass(frozen=True)
class OfflineState:
    """The full public read of offline mode — what the route serializes for the UI."""

    manual: bool
    auto: bool
    online: bool
    effective_offline: bool


class OfflineModeService:
    """Owns the offline-mode state machine and the connectivity monitor.

    ``probe`` is injectable so tests drive the state machine without touching the
    network; the default checks the configured public anchors. ``auto_default`` seeds
    the auto-detect switch on first run (before any persisted value exists).
    """

    def __init__(
        self,
        *,
        searxng: _GatedService,
        browser: _GatedService,
        settings_store: SettingsStore,
        owner_id: str,
        anchors: list[str],
        interval_s: float,
        timeout_s: float,
        fail_threshold: int,
        recover_threshold: int,
        auto_default: bool = True,
        probe: Callable[[], Awaitable[bool]] | None = None,
        network_tools: frozenset[str] = frozenset(),
    ) -> None:
        self._searxng = searxng
        self._browser = browser
        self._network_tools = network_tools
        self._settings = settings_store
        self._owner = owner_id
        self._interval_s = interval_s
        self._fail_threshold = max(1, fail_threshold)
        self._recover_threshold = max(1, recover_threshold)
        self._auto_default = auto_default
        self._probe = probe or (lambda: check_online(anchors, timeout_s))

        self._manual = False
        self._auto = auto_default
        self._online = True  # set for real by the boot probe in start()
        # None until the first apply, so the boot apply always acts (the services
        # are not running yet — there is no prior effective state to compare against).
        self._effective_offline: bool | None = None
        self._fail = 0
        self._ok = 0
        self._task: asyncio.Task[None] | None = None
        # Serializes service start/stop so a manual toggle can't race a monitor
        # transition into a double bring-up or a torn-down-then-half-started state.
        self._lock = asyncio.Lock()

    # --- lifecycle --------------------------------------------------------

    async def start(self) -> None:
        """Load persisted switches, probe once (offline until proven online), bring the
        services up iff online, then launch the monitor. Replaces the direct
        ``searxng.start()``/``browser.start()`` calls in the app lifespan."""
        manual_raw = await self._settings.get(self._owner, OFFLINE_MANUAL_KEY)
        auto_raw = await self._settings.get(self._owner, OFFLINE_AUTO_KEY)
        self._manual = manual_raw == "true"
        self._auto = (auto_raw == "true") if auto_raw is not None else self._auto_default

        self._online = await self._safe_probe()
        logger.info(
            "offline mode: boot probe → %s (manual=%s, auto=%s)",
            "online" if self._online else "offline",
            self._manual,
            self._auto,
        )
        await self._apply()
        self._task = asyncio.create_task(self._monitor())

    async def stop(self) -> None:
        """Cancel the monitor. The services are stopped by the lifespan's own
        teardown (this service doesn't own their final shutdown)."""
        await cancel_and_wait(self._task, STOP_TIMEOUT_S)
        self._task = None

    # --- reads ------------------------------------------------------------

    def state(self) -> OfflineState:
        return OfflineState(
            manual=self._manual,
            auto=self._auto,
            online=self._online,
            effective_offline=bool(self._effective_offline),
        )

    def web_tools_disabled(self) -> frozenset[str]:
        """The network-dependent tool names to hide from the agent while offline (empty
        when online).

        The set comes from the feature manifests — each declares which of its own tools
        need the internet — not from a list here. Offline mode's job is to suspend
        network-dependent capabilities; which tools those *are* is knowledge the feature
        that ships them owns, and a copy here would go stale the first time a feature grew
        another one, leaving a dead tool on offer."""
        return self._network_tools if self._effective_offline else frozenset()

    # --- operator switches ------------------------------------------------

    async def set_manual(self, value: bool) -> OfflineState:
        self._manual = value
        await self._settings.set(self._owner, OFFLINE_MANUAL_KEY, _flag(value))
        await self._apply()
        return self.state()

    async def set_auto(self, value: bool) -> OfflineState:
        self._auto = value
        await self._settings.set(self._owner, OFFLINE_AUTO_KEY, _flag(value))
        await self._apply()
        return self.state()

    # --- monitor ----------------------------------------------------------

    async def _monitor(self) -> None:
        """Probe on the interval forever; fold each sample into the debounced verdict.

        Runs unconditionally (the probe is a cheap TCP connect) so the reported status
        stays live even when auto-detect is off or the operator is manually offline —
        only the *action* (toggling the services) is gated, by the effective-state math."""
        while True:
            await asyncio.sleep(self._interval_s)
            try:
                await self._observe(await self._safe_probe())
            except Exception:
                # The heartbeat must outlive a transient error on the service-toggle
                # path; if it died, connectivity would stop being tracked silently.
                logger.exception("offline mode: monitor iteration failed")

    async def _observe(self, online: bool) -> None:
        """Fold one connectivity sample into the debounced ``online`` verdict, applying a
        service transition when the streak crosses a threshold. Split out from the
        monitor loop so the state machine is unit-testable without real timers."""
        if online:
            self._ok += 1
            self._fail = 0
            if not self._online and self._ok >= self._recover_threshold:
                self._online = True
                logger.info("offline mode: connectivity recovered")
                await self._apply()
        else:
            self._fail += 1
            self._ok = 0
            if self._online and self._fail >= self._fail_threshold:
                self._online = False
                logger.info("offline mode: connectivity lost (%d consecutive failures)", self._fail)
                await self._apply()

    async def _apply(self) -> None:
        """Recompute the effective verdict and, on a change, toggle the services."""
        async with self._lock:
            new = self._manual or (self._auto and not self._online)
            if new == self._effective_offline:
                return
            self._effective_offline = new
            if new:
                logger.info("offline mode: ON — suspending web services")
                await self._searxng.stop()
                await self._browser.stop()
            else:
                logger.info("offline mode: OFF — resuming web services")
                await self._searxng.start()
                await self._browser.start()

    async def _safe_probe(self) -> bool:
        try:
            return await self._probe()
        except Exception:
            # A probe must never crash the monitor; treat an error as "can't reach it".
            logger.exception("offline mode: connectivity probe raised")
            return False
