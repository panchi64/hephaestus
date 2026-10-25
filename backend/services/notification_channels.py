"""Out-of-band notification channels: the contract, and the push webhook core ships.

The in-app surface (`services/notifications.py`) is always on: everything the workspace
records shows up in the app. These channels reach the operator when the app isn't in front
of them, which is the whole point of an unattended run parking for approval.

- **A channel is a seam, not a hard-coded vendor.** :class:`NotificationChannel` is the
  whole contract. Core ships push; a feature adds its own at build
  (``NotificationService.add_channel``), as mail does for email.
- **A channel never breaks the thing that notified.** Delivery runs on the notification
  service's lock-aware drainer, off the critical path; a channel that is unconfigured or
  unreachable degrades to in-app-only rather than failing the park or the reminder.
- **Only interruption-worthy kinds leave the machine.** Every notification is in-app; only
  the kinds in :data:`OUT_OF_BAND_KINDS` are worth reaching for someone's phone over.

The push channel is a **webhook**, not a browser Web Push subscription: the operator points
it at whatever notifier they already run (ntfy, Gotify, Pushover, Home Assistant), so a
notification reaches a phone with the app closed. Its URL is operator-configured, so it is
checked for shape only and a LAN address is allowed on purpose. Nothing model-supplied
ever reaches it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit

import httpx

from core.declarations import DeclaredSet
from core.vault import Vault
from services.notification_view import NotificationView
from services.settings_store import SettingsStore

logger = logging.getLogger(__name__)

CHANNEL_PUSH = "push"

#: The kinds worth interrupting the operator away from the app for. A run stopped dead
#: waiting on them is core's; a feature adds its own (``FeatureManifest.out_of_band_kinds``).
#: Informational kinds stay in-app: mailing every one would train the operator to ignore it.
OUT_OF_BAND_KINDS: DeclaredSet[str] = DeclaredSet(frozenset({"approval_needed"}))

# Owner-scoped preference keys. The endpoint is vault-sealed before it is stored: its path
# is often the only credential, and the settings table is otherwise plaintext.
PUSH_ENABLED_KEY = "notifications.push.enabled"
PUSH_ENDPOINT_KEY = "notifications.push.endpoint_enc"

_PUSH_TIMEOUT_S = 15.0
_ALLOWED_SCHEMES = frozenset({"http", "https"})
# Push notifiers truncate anyway, and a notification body can be a whole run summary.
_PUSH_BODY_MAX = 1_000


@dataclass(frozen=True, slots=True)
class ChannelHealth:
    """One channel as the operator sees it.

    ``status``/``detail`` are decided here and rendered verbatim — the overview route maps
    them onto its own row shape without re-deciding what counts as healthy.
    """

    key: str
    label: str
    configured: bool
    status: str  # "nominal" | "warn" | "alert"
    detail: str


class NotificationChannel(Protocol):
    """The whole contract a delivery channel implements."""

    key: str
    label: str

    async def health(self, owner_id: str) -> ChannelHealth:
        """How this channel is doing, for the operator's health surface. Never raises —
        an unreachable dependency is a *reported* state, not an exception."""
        ...

    async def deliver(self, view: NotificationView) -> None:
        """Deliver one notification. Raises on a transient failure so the drainer retries;
        returns without delivering when the channel simply isn't configured."""
        ...

    async def close(self) -> None:
        """Release anything this channel opened for itself (a pooled HTTP client, a
        connection). Called once at shutdown; a channel that borrows every resource it
        uses implements this as a no-op."""
        ...


class PushChannel:
    """Delivers by POSTing to the operator's own push endpoint.

    The payload carries the notification under **both** ``title``/``message`` (what Gotify
    and ntfy read) and ``body``/``kind`` (for a generic webhook), plus ntfy's ``Title`` and
    ``Priority`` headers — so pointing this at a notifier works without a per-vendor adapter.
    Delivery failures raise, so the drainer retries with backoff and gives up loudly rather
    than pretending the operator was reached.
    """

    key: str = CHANNEL_PUSH
    label: str = "PUSH ALERTS"

    def __init__(
        self,
        settings: SettingsStore,
        vault: Vault,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings: SettingsStore = settings
        self._vault: Vault = vault
        self._client: httpx.AsyncClient | None = client
        self._owns_client: bool = client is None

    async def health(self, owner_id: str) -> ChannelHealth:
        try:
            endpoint = await self._endpoint(owner_id)
        except Exception:  # health never raises
            logger.warning("notifications: could not read the push endpoint", exc_info=True)
            endpoint = None
        if endpoint is None:
            return ChannelHealth(self.key, self.label, False, "warn", "no endpoint — in-app only")
        if not await self._enabled(owner_id):
            return ChannelHealth(self.key, self.label, False, "warn", "turned off")
        # The host, never the path — a push endpoint's path is usually its credential.
        host = urlsplit(endpoint).hostname or "configured"
        return ChannelHealth(self.key, self.label, True, "nominal", f"to {host}")

    async def deliver(self, view: NotificationView) -> None:
        endpoint = await self._endpoint(view.owner_id)
        if endpoint is None or not await self._enabled(view.owner_id):
            return
        body = (view.body or view.title)[:_PUSH_BODY_MAX]
        response = await self._http().post(
            endpoint,
            json={
                "title": view.title,
                "message": body,
                "body": body,
                "kind": view.kind,
                "id": view.id,
            },
            headers={"Title": view.title, "Priority": "high"},
        )
        if response.status_code >= 400:
            raise RuntimeError(f"push endpoint returned HTTP {response.status_code}")

    async def _enabled(self, owner_id: str) -> bool:
        return await self._settings.get(owner_id, PUSH_ENABLED_KEY, "true") != "false"

    async def _endpoint(self, owner_id: str) -> str | None:
        sealed = await self._settings.get(owner_id, PUSH_ENDPOINT_KEY)
        return self._vault.decrypt_str(sealed) if sealed else None

    async def configure(self, owner_id: str, *, enabled: bool, endpoint: str | None) -> None:
        """Persist the endpoint (sealed) and the on/off switch. The URL is validated for
        shape here — scheme and a host, with embedded credentials refused so a typo can't
        ship a password to a third party. A private/LAN address is allowed on purpose: the
        operator's own notifier is the expected target."""
        await self._settings.set(owner_id, PUSH_ENABLED_KEY, "true" if enabled else "false")
        if endpoint is None:
            return
        parts = urlsplit(endpoint)
        if parts.scheme not in _ALLOWED_SCHEMES:
            raise ValueError("a push endpoint must be an http(s) URL")
        if not parts.hostname:
            raise ValueError("a push endpoint needs a host")
        if parts.username or parts.password:
            raise ValueError("a push endpoint must not embed credentials in the URL")
        await self._settings.set(owner_id, PUSH_ENDPOINT_KEY, self._vault.encrypt_str(endpoint))

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=_PUSH_TIMEOUT_S)
            self._owns_client = True
        return self._client

    async def close(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
        self._client = None


def default_channels(settings: SettingsStore, vault: Vault) -> list[NotificationChannel]:
    """The channels core ships; features add theirs with ``add_channel``."""
    return [PushChannel(settings, vault)]
