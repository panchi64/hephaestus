"""The email notification channel: alerts delivered through the operator's own mailbox."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Protocol

from core.vault import Vault
from services.notification_channels import ChannelHealth
from services.notification_view import NotificationView
from services.settings_store import SettingsStore

if TYPE_CHECKING:
    from .service import AccountView

logger = logging.getLogger(__name__)

CHANNEL_EMAIL = "email"

# Owner-scoped preference keys. The address is vault-sealed before it is stored: the
# settings table is otherwise plaintext.
EMAIL_ENABLED_KEY = "notifications.email.enabled"
EMAIL_ADDRESS_KEY = "notifications.email.address_enc"
EMAIL_ACCOUNT_KEY = "notifications.email.account_id"


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
    """Delivers through the operator's own mail account.

    Configuration-free by default: with a mail account connected, the workspace mails the
    operator at that account's own address, from that account. There is exactly one
    operator, so their mailbox is not a guess. An explicit address/account override wins
    when set, and disabling the channel is honored either way.
    """

    key: str = CHANNEL_EMAIL
    label: str = "EMAIL ALERTS"

    def __init__(self, mail: MailSender, settings: SettingsStore, vault: Vault) -> None:
        self._mail: MailSender = mail
        self._settings: SettingsStore = settings
        self._vault: Vault = vault

    async def health(self, owner_id: str) -> ChannelHealth:
        if not await self._enabled(owner_id):
            return ChannelHealth(self.key, self.label, False, "warn", "turned off")
        try:
            target = await self._target(owner_id)
        except Exception:  # health never raises; a broken read is a state
            logger.warning("notifications: could not read the email channel target", exc_info=True)
            target = None
        if target is None:
            return ChannelHealth(
                self.key, self.label, False, "warn", "no mail account — in-app only"
            )
        _account_id, address = target
        return ChannelHealth(self.key, self.label, True, "nominal", f"to {address}")

    async def deliver(self, view: NotificationView) -> None:
        if not await self._enabled(view.owner_id):
            return
        target = await self._target(view.owner_id)
        if target is None:
            return
        account_id, address = target
        _ = await self._mail.send(
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
        accounts = [a for a in await self._mail.list_accounts(owner_id) if a.enabled]
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
        """Nothing to release: the mail service owns and closes its own transports."""
