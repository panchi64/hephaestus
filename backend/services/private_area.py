"""The Private area: threads behind a PIN, hidden from the listing, the API and every agent
while locked.

Unlocking holds the area open for a sliding window that each use of a private thread
extends. The open state lives only in memory, so a restart, a vault lock or logout closes
it. The PIN guards access, not storage: private threads are sealed under the vault key like
every other thread, so the PIN needs no key of its own and resetting it with the account
password loses nothing.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from core import crypto
from core.exceptions import InvalidInputError, PermissionDeniedError, PrivateAreaLocked
from core.vault import Vault
from models._fields import utcnow
from services.conversations import ConversationStore
from services.settings_store import SettingsStore

PIN_VERIFIER_KEY = "private.pin_verifier"
RELOCK_MINUTES_KEY = "private.relock_minutes"
DEFAULT_RELOCK_MINUTES = 15
RELOCK_MINUTES_MAX = 720
#: Wrong PINs in a row that lock the whole vault, so a PIN can't be guessed past a session.
MAX_FAILED_UNLOCKS = 5

_PIN = re.compile(r"\d{4,12}")


@dataclass(frozen=True)
class PrivateStatus:
    pin_set: bool
    unlocked: bool
    #: When the open area closes unless used again; None while locked.
    relock_at: datetime | None
    relock_minutes: int


class PrivateArea:
    def __init__(
        self,
        vault: Vault,
        settings: SettingsStore,
        conversations: ConversationStore,
        *,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._vault: Vault = vault
        self._settings: SettingsStore = settings
        self._conversations: ConversationStore = conversations
        self._clock: Callable[[], datetime] = clock
        self._unlocked_until: datetime | None = None
        self._window: timedelta = timedelta(minutes=DEFAULT_RELOCK_MINUTES)
        self._failures: int = 0

    def is_unlocked(self) -> bool:
        return self._unlocked_until is not None and self._clock() < self._unlocked_until

    async def status(self, owner_id: str) -> PrivateStatus:
        unlocked = self.is_unlocked()
        return PrivateStatus(
            pin_set=await self._verifier(owner_id) is not None,
            unlocked=unlocked,
            relock_at=self._unlocked_until if unlocked else None,
            relock_minutes=await self.relock_minutes(owner_id),
        )

    async def set_pin(
        self,
        owner_id: str,
        pin: str,
        *,
        current_pin: str | None = None,
        password: str | None = None,
    ) -> None:
        """Set the PIN, or replace it given the current PIN or the account password, and
        open the area.

        Raises:
            InvalidInputError: ``pin`` is not 4 to 12 digits.
            PermissionDeniedError: a PIN is set and neither proof checks out.
        """
        if not _PIN.fullmatch(pin):
            raise InvalidInputError("The PIN must be 4 to 12 digits.")
        existing = await self._verifier(owner_id)
        if existing is not None:
            by_pin = current_pin is not None and await _verify(existing, current_pin)
            by_password = password is not None and await self._vault.verify_password(password)
            if not (by_pin or by_password):
                self._count_failure()
                raise PermissionDeniedError("The current PIN or password is wrong.")
        verifier = await asyncio.to_thread(crypto.hash_password, pin)
        await self._settings.set(owner_id, PIN_VERIFIER_KEY, self._vault.encrypt_str(verifier))
        self._failures = 0
        await self._open(owner_id)

    async def unlock(self, owner_id: str, pin: str) -> bool:
        """Open the area. False on a wrong PIN; the fifth in a row locks the vault."""
        verifier = await self._verifier(owner_id)
        if verifier is None or not await _verify(verifier, pin):
            self._count_failure()
            return False
        self._failures = 0
        await self._open(owner_id)
        return True

    def _count_failure(self) -> None:
        """Wrong proofs from unlock and PIN change share one count, so neither path can
        guess past the vault lock."""
        self._failures += 1
        if self._failures >= MAX_FAILED_UNLOCKS:
            self._failures = 0
            self._vault.lock()

    async def _open(self, owner_id: str) -> None:
        self._window = timedelta(minutes=await self.relock_minutes(owner_id))
        self._unlocked_until = self._clock() + self._window

    def lock(self) -> None:
        self._unlocked_until = None

    def touch(self) -> None:
        """Hold an open area open for another window; a locked one stays locked."""
        if self.is_unlocked():
            self._unlocked_until = self._clock() + self._window

    async def relock_minutes(self, owner_id: str) -> int:
        raw = await self._settings.get(owner_id, RELOCK_MINUTES_KEY)
        try:
            minutes = int(raw) if raw is not None else DEFAULT_RELOCK_MINUTES
        except ValueError:
            return DEFAULT_RELOCK_MINUTES
        return _bounded(minutes)

    async def set_relock_minutes(self, owner_id: str, minutes: int) -> int:
        bounded = _bounded(minutes)
        await self._settings.set(owner_id, RELOCK_MINUTES_KEY, str(bounded))
        self._window = timedelta(minutes=bounded)
        if self.is_unlocked():
            self._unlocked_until = self._clock() + self._window
        return bounded

    async def is_private(self, conversation_id: str) -> bool:
        return await self._conversations.is_private(conversation_id)

    async def title(self, owner_id: str, conversation_id: str) -> str | None:
        """A thread's title for an approval card; recall tools hold no conversation store."""
        return (await self._conversations.titles([conversation_id], owner_id)).get(conversation_id)

    async def require_open(self, conversation_id: str, *, by_operator: bool = True) -> None:
        """Refuse a private thread while the area is locked, or to anyone but the operator's
        own session, and keep an open area open.

        Raises:
            PrivateAreaLocked: the thread is private and the area is locked or the caller
                is not the operator.
        """
        if not await self.is_private(conversation_id):
            return
        if not (by_operator and self.is_unlocked()):
            raise PrivateAreaLocked
        self.touch()

    async def set_private(self, owner_id: str, conversation_id: str, private: bool) -> None:
        """File a thread in or out of the area.

        Raises:
            PrivateAreaLocked: the area is locked.
            InvalidInputError: filing a thread in with no PIN set.
        """
        await self.require_writable(owner_id, filing_in=private)
        await self._conversations.set_private(conversation_id, private)

    async def require_writable(self, owner_id: str, *, filing_in: bool = True) -> None:
        """Refuse to file a thread in or out unless the area is open, and to file one in
        before a PIN is set.

        Raises:
            InvalidInputError: filing in with no PIN set.
            PrivateAreaLocked: the area is locked.
        """
        if filing_in and await self._verifier(owner_id) is None:
            raise InvalidInputError("Set a PIN before making a chat private.")
        if not self.is_unlocked():
            raise PrivateAreaLocked
        self.touch()

    async def _verifier(self, owner_id: str) -> str | None:
        sealed = await self._settings.get(owner_id, PIN_VERIFIER_KEY)
        return self._vault.decrypt_str(sealed) if sealed else None


def _bounded(minutes: int) -> int:
    return min(max(minutes, 1), RELOCK_MINUTES_MAX)


async def _verify(verifier: str, pin: str) -> bool:
    return await asyncio.to_thread(crypto.verify_password, verifier, pin)
