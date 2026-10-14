"""Out-of-band notification channels — email and push (`AE-3.2`, `TASK-6`).

The in-app surface (`services/notifications.py`) is always on: everything the workspace
records shows up in the app. These channels are the *other* half — the ones that reach the
operator when the app isn't in front of them, which is the whole point of an unattended run
parking for approval (`AE-3.2`) or a reminder firing on its date (`TASK-6`).

Three rules shape the design:

- **A channel is a seam, not a hard-coded vendor.** :class:`NotificationChannel` is the
  whole contract — say whether you're configured, and deliver one notification. Email and
  push are the two implementations; an SMS or Matrix channel later is a new class here and
  nothing else.
- **A channel never breaks the thing that notified.** Delivery runs on the notification
  service's lock-aware drainer, off the critical path; a channel that is unconfigured or
  unreachable degrades to in-app-only rather than failing the park or the reminder.
- **Only interruption-worthy kinds leave the machine.** Every notification is in-app; only
  the kinds in :data:`OUT_OF_BAND_KINDS` are worth reaching for someone's phone over.

The push channel is deliberately a **webhook**, not a browser Web Push subscription: the
operator points it at whatever notifier they already run (ntfy, Gotify, Pushover, Home
Assistant, a Shortcuts endpoint), so a notification reaches a phone with the app closed and
no browser, no vendor account, and no service worker in the loop. Its URL is
operator-configured — the same trust level as the IMAP host they typed — so it is checked
for shape only and a LAN address is allowed on purpose, since a self-hosted notifier
usually *is* on the LAN. Nothing model-supplied ever reaches it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol
from urllib.parse import urlsplit

import httpx

from core.vault import Vault
from services.notification_view import NotificationView
from services.settings_store import SettingsStore

if TYPE_CHECKING:
    from collections.abc import Callable

    from services.mail import AccountView

logger = logging.getLogger(__name__)

CHANNEL_EMAIL = "email"
CHANNEL_PUSH = "push"

# The kinds worth interrupting the operator away from the app for: a run that has stopped
# dead waiting on them (`AE-3.2` — an unattended run parks until answered) and a reminder,
# which is worthless if it only lands somewhere they aren't looking (`TASK-6`). Run
# outcomes, task outcomes and triage alerts stay in-app: they are informational, and mailing
# every one of them would train the operator to ignore the channel.
OUT_OF_BAND_KINDS = frozenset({"approval_needed", "reminder"})

# Owner-scoped preference keys (`services/settings_store.py`). The two address-shaped values
# are vault-sealed before they are stored — a delivery address and a push endpoint (whose
# path is often the only credential) are the operator's data like anything else, and the
# settings table is otherwise plaintext.
EMAIL_ENABLED_KEY = "notifications.email.enabled"
EMAIL_ADDRESS_KEY = "notifications.email.address_enc"
EMAIL_ACCOUNT_KEY = "notifications.email.account_id"
PUSH_ENABLED_KEY = "notifications.push.enabled"
PUSH_ENDPOINT_KEY = "notifications.push.endpoint_enc"

_PUSH_TIMEOUT_S = 15.0
_ALLOWED_SCHEMES = frozenset({"http", "https"})
# Push notifiers truncate anyway, and a notification body can be a whole run summary.
_PUSH_BODY_MAX = 1_000


@dataclass(frozen=True, slots=True)
class ChannelHealth:
    """One channel as the operator sees it (`XC-DEG-3`).

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


class MailSender(Protocol):
    """The slice of the mail service the email channel lists accounts and sends through."""

    async def list_accounts(self, owner_id: str) -> list[AccountView]: ...

    async def send(
        self,
        owner_id: str,
        account_id: str,
        *,
        to: list[str],
        subject: str,
        body: str,
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
        in_reply_to: str | None = None,
    ) -> str: ...


class EmailChannel:
    """Delivers through the operator's own mail account (`EMAIL-1`).

    Configuration-free by default: with a mail account connected, the workspace mails the
    operator at that account's own address, from that account. There is exactly one operator
    (`XC-SEC-*`), so their mailbox is not a guess. An explicit address/account override wins
    when set, and disabling the channel is honored either way.
    """

    key: str = CHANNEL_EMAIL
    label: str = "EMAIL ALERTS"

    def __init__(
        self,
        mail: Callable[[], MailSender | None],
        settings: SettingsStore,
        vault: Vault,
    ) -> None:
        self._mail: Callable[[], MailSender | None] = mail
        self._settings: SettingsStore = settings
        self._vault: Vault = vault

    async def health(self, owner_id: str) -> ChannelHealth:
        service = self._mail()
        if service is None:
            return ChannelHealth(
                self.key, self.label, False, "warn", "no mail account — in-app only"
            )
        if not await self._enabled(owner_id):
            return ChannelHealth(self.key, self.label, False, "warn", "turned off")
        try:
            target = await self._target(owner_id)
        except Exception:  # noqa: BLE001 — health never raises; a broken read is a state
            logger.warning("notifications: could not read the email channel target", exc_info=True)
            target = None
        if target is None:
            return ChannelHealth(
                self.key, self.label, False, "warn", "no mail account — in-app only"
            )
        _account_id, address = target
        return ChannelHealth(self.key, self.label, True, "nominal", f"to {address}")

    async def deliver(self, view: NotificationView) -> None:
        service = self._mail()
        if service is None or not await self._enabled(view.owner_id):
            return
        target = await self._target(view.owner_id)
        if target is None:
            return
        account_id, address = target
        _ = await service.send(
            view.owner_id,
            account_id,
            to=[address],
            subject=view.title,
            body=view.body or view.title,
        )

    async def _enabled(self, owner_id: str) -> bool:
        """On unless explicitly turned off — a connected mailbox is consent enough to be
        told that a run is stuck waiting on you."""
        return await self._settings.get(owner_id, EMAIL_ENABLED_KEY, "true") != "false"

    async def _target(self, owner_id: str) -> tuple[str, str] | None:
        """``(account to send from, address to send to)``, or ``None`` when there is no
        usable mailbox. The stored override is used only when its account still exists —
        a deleted account falls back rather than sending nowhere."""
        service = self._mail()
        if service is None:
            return None
        accounts = [a for a in await service.list_accounts(owner_id) if a.enabled]
        if not accounts:
            return None
        stored_id = await self._settings.get(owner_id, EMAIL_ACCOUNT_KEY)
        account = next((a for a in accounts if a.id == stored_id), accounts[0])
        sealed = await self._settings.get(owner_id, EMAIL_ADDRESS_KEY)
        address = self._vault.decrypt_str(sealed) if sealed else account.address
        return account.id, address

    async def configure(
        self, owner_id: str, *, enabled: bool, address: str | None, account_id: str | None
    ) -> None:
        """Persist the operator's choices. The address is sealed on the way in and is never
        read back out to a caller — only used to address a delivery."""
        await self._settings.set(owner_id, EMAIL_ENABLED_KEY, "true" if enabled else "false")
        if address is not None:
            await self._settings.set(owner_id, EMAIL_ADDRESS_KEY, self._vault.encrypt_str(address))
        if account_id is not None:
            await self._settings.set(owner_id, EMAIL_ACCOUNT_KEY, account_id)

    async def close(self) -> None:
        """Nothing to release — this channel borrows the mail service, which owns (and
        closes) its own transports."""


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
        except Exception:  # noqa: BLE001 — health never raises
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


def default_channels(
    mail: Callable[[], MailSender | None], settings: SettingsStore, vault: Vault
) -> list[NotificationChannel]:
    """The channels every workspace gets, in the order the operator sees them."""
    return [EmailChannel(mail, settings, vault), PushChannel(settings, vault)]
