"""The password vault — the operator's secrets manager (`VAULT-1`).

**Not** ``core/vault``. That one is the password-derived at-rest key custody that unlocks the
whole app at login. This is the user-facing place the operator keeps credentials, and it has
**its own lock**: its own passphrase, its own Argon2id salt, its own key. Unlocking the app
does not unlock this; locking this does not lock the app. The one direction that *does*
couple them is the safe one — when the app locks or the operator logs out, every vault
session in the process is torn down with it (registered on the login vault's lock signal,
the same way the operator shell tears down its PTYs).

**The unlocked state is memory-only and never persisted.** A session lives in
``_sessions`` — the derived key plus an idle deadline — and nothing about it reaches the DB.
A process restart therefore starts locked no matter what, because the only place the key
existed is gone. The DB holds solely what is needed to *re-derive* it from a passphrase the
operator supplies again (``SecretVaultConfig``).

**Two layers, not one.** Every stored value is sealed under this vault's key and the
resulting token is then sealed again by the login DEK, so an entry is readable only with both
locks open — the "additional encrypted layer on top of at-rest encryption" the spec asks for.

Raises domain errors only (below); the route maps them to HTTP.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Engine
from sqlmodel import Session, select

from core import crypto
from core.db import in_session
from core.exceptions import NotFoundError, OdysseusError
from core.vault import Vault
from models._fields import utcnow
from models.secret import SecretEntry, SecretVaultConfig

logger = logging.getLogger(__name__)

# How long an unlocked vault stays open without being read. A secrets manager left open
# forever is a secrets manager with no lock; every read slides the deadline forward, so
# active use never trips it. Constructor-overridable so tests drive it directly.
_IDLE_TIMEOUT_S = 15 * 60.0


class SecretVaultLocked(OdysseusError):
    """The secrets manager is locked (or was never unlocked in this process)."""


class SecretVaultNotConfigured(OdysseusError):
    """No vault has been set up for this operator yet."""


class SecretVaultAlreadyConfigured(OdysseusError):
    """A vault already exists; configuring twice would strand every stored entry."""


@dataclass(frozen=True)
class SecretVaultStatus:
    """What the operator may do next — configure, unlock, or read."""

    configured: bool
    unlocked: bool


@dataclass(frozen=True)
class SecretEntryView:
    """One decrypted credential. Only ever produced while the vault is unlocked."""

    id: str
    name: str
    username: str
    url: str
    password: str
    created_at: datetime
    updated_at: datetime


@dataclass
class _Session:
    """An unlocked vault, in memory only. Never written, never serialized."""

    key: bytes
    expires_at: float


def _b64e(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def _b64d(text: str) -> bytes:
    return base64.b64decode(text)


class SecretVaultService:
    def __init__(
        self,
        engine: Engine,
        vault: Vault,
        *,
        idle_timeout_s: float = _IDLE_TIMEOUT_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._engine = engine
        self._vault = vault
        self._idle_timeout_s = idle_timeout_s
        self._clock = clock
        # owner id → its live session. The only home the vault key ever has.
        self._sessions: dict[str, _Session] = {}
        # An app-level lock/logout ends every vault session too: the login DEK is gone, so
        # nothing is readable anyway, and leaving a derived key behind after the operator
        # locked up would be the one place a secret outlives its lock.
        vault.register_on_lock(self.logout)

    # --- lifecycle (`VAULT-1`) ------------------------------------------------------

    async def status(self, owner_id: str) -> SecretVaultStatus:
        """Whether the vault is configured and open — an observation, not a use, so it
        does NOT slide the idle deadline (the UI polls this to render the lock indicator;
        counting a poll as activity would keep the vault open indefinitely)."""
        config = await self._config(owner_id)
        return SecretVaultStatus(
            configured=config is not None,
            unlocked=self._live_key(owner_id, touch=False) is not None,
        )

    async def configure(self, owner_id: str, passphrase: str) -> None:
        """First run: mint a vault key, wrap it under the passphrase, and leave the vault
        unlocked — mirroring ``core.vault.Vault.setup``, with an independent salt so this
        passphrase derives nothing the login password does (and vice versa)."""
        if not passphrase:
            raise ValueError("passphrase must not be empty")
        if await self._config(owner_id) is not None:
            raise SecretVaultAlreadyConfigured("the password vault is already configured")

        key = crypto.generate_dek()
        salt = crypto.generate_salt()
        kek = await asyncio.to_thread(crypto.derive_kek, passphrase, salt)
        # Threaded like `derive_kek`: the same Argon2 cost, and inline it would stall
        # every other connection on the loop for its duration.
        verifier = await asyncio.to_thread(crypto.hash_password, passphrase)
        row = SecretVaultConfig(
            owner_id=owner_id,
            verifier=verifier,
            kek_salt=_b64e(salt),
            wrapped_key_enc=self._vault.encrypt_str(_b64e(crypto.aead_encrypt(kek, key))),
        )

        def work(session: Session) -> None:
            session.add(row)
            session.flush()

        await in_session(self._engine, work)
        self._open_session(owner_id, key)

    async def unlock(self, owner_id: str, passphrase: str) -> bool:
        """Verify the passphrase and unwrap the vault key into memory. False on a bad
        passphrase — indistinguishable from a key that won't unwrap, so a tampered
        wrapped key reads as a failed unlock rather than a crash."""
        config = await self._config(owner_id)
        if config is None:
            raise SecretVaultNotConfigured("the password vault has not been set up")
        if not await asyncio.to_thread(crypto.verify_password, config.verifier, passphrase):
            return False
        kek = await asyncio.to_thread(crypto.derive_kek, passphrase, _b64d(config.kek_salt))
        try:
            key = crypto.aead_decrypt(kek, _b64d(self._vault.decrypt_str(config.wrapped_key_enc)))
        except Exception:  # noqa: BLE001 — any unwrap failure is just a failed unlock
            return False
        self._open_session(owner_id, key)
        return True

    def lock(self, owner_id: str) -> None:
        """Re-seal this operator's vault: the key is dropped from memory. Idempotent."""
        self._sessions.pop(owner_id, None)

    def logout(self) -> None:
        """End every vault session in this process — the operator's explicit log out, and
        what the login vault's lock signal fires. Broader than :meth:`lock`, which closes
        one operator's vault and leaves any other session alone."""
        self._sessions.clear()

    # --- entries --------------------------------------------------------------------

    async def list_entries(self, owner_id: str) -> list[SecretEntryView]:
        key = self._require_key(owner_id)

        def work(session: Session) -> list[SecretEntry]:
            return list(
                session.exec(
                    select(SecretEntry)
                    .where(SecretEntry.owner_id == owner_id)
                    .order_by(SecretEntry.created_at)  # type: ignore[arg-type]
                ).all()
            )

        rows = await in_session(self._engine, work)
        return [self._view(key, row) for row in rows]

    async def get(self, owner_id: str, entry_id: str) -> SecretEntryView:
        key = self._require_key(owner_id)
        return self._view(key, await self._row(owner_id, entry_id))

    async def create(
        self,
        owner_id: str,
        *,
        name: str,
        username: str = "",
        url: str = "",
        password: str = "",
    ) -> SecretEntryView:
        key = self._require_key(owner_id)
        if not name.strip():
            raise ValueError("name must not be empty")
        row = SecretEntry(
            owner_id=owner_id,
            name_enc=self._seal(key, name),
            username_enc=self._seal(key, username),
            url_enc=self._seal(key, url),
            password_enc=self._seal(key, password),
        )

        def work(session: Session) -> None:
            session.add(row)
            session.flush()

        await in_session(self._engine, work)
        return self._view(key, row)

    async def update(
        self,
        owner_id: str,
        entry_id: str,
        *,
        name: str | None = None,
        username: str | None = None,
        url: str | None = None,
        password: str | None = None,
    ) -> SecretEntryView:
        key = self._require_key(owner_id)
        row = await self._row(owner_id, entry_id)
        if name is not None:
            if not name.strip():
                raise ValueError("name must not be empty")
            row.name_enc = self._seal(key, name)
        if username is not None:
            row.username_enc = self._seal(key, username)
        if url is not None:
            row.url_enc = self._seal(key, url)
        if password is not None:
            row.password_enc = self._seal(key, password)
        row.updated_at = utcnow()

        def work(session: Session) -> None:
            session.add(row)
            session.flush()

        await in_session(self._engine, work)
        return self._view(key, row)

    async def delete(self, owner_id: str, entry_id: str) -> None:
        # Deleting doesn't read the secret, but it is still a vault operation — a locked
        # vault must not be editable from outside, or "locked" means very little.
        self._require_key(owner_id)
        row = await self._row(owner_id, entry_id)

        def work(session: Session) -> None:
            session.delete(session.merge(row))

        await in_session(self._engine, work)

    # --- internals ------------------------------------------------------------------

    async def _config(self, owner_id: str) -> SecretVaultConfig | None:
        def work(session: Session) -> SecretVaultConfig | None:
            return session.exec(
                select(SecretVaultConfig).where(SecretVaultConfig.owner_id == owner_id)
            ).first()

        return await in_session(self._engine, work)

    async def _row(self, owner_id: str, entry_id: str) -> SecretEntry:
        def work(session: Session) -> SecretEntry | None:
            return session.exec(
                select(SecretEntry)
                .where(SecretEntry.owner_id == owner_id)
                .where(SecretEntry.id == entry_id)
            ).first()

        row = await in_session(self._engine, work)
        if row is None:
            raise NotFoundError(f"vault entry {entry_id!r} not found")
        return row

    def _open_session(self, owner_id: str, key: bytes) -> None:
        self._sessions[owner_id] = _Session(
            key=key, expires_at=self._clock() + self._idle_timeout_s
        )

    def _live_key(self, owner_id: str, *, touch: bool = True) -> bytes | None:
        """The session key if the vault is open, else None — expiring the session in
        passing. ``touch`` slides the idle deadline forward, so a vault in active use
        never locks under the operator, and one left alone closes itself.

        ``touch=False`` is for *observing* the state rather than using the key. It matters
        because the UI polls the lock indicator: counting a poll as use would slide the
        deadline on every tick, and a vault the operator walked away from would stay open
        for as long as a browser tab is pointed at it — the auto-relock would never fire.
        """
        session = self._sessions.get(owner_id)
        if session is None:
            return None
        if self._clock() >= session.expires_at:
            del self._sessions[owner_id]
            return None
        if touch:
            session.expires_at = self._clock() + self._idle_timeout_s
        return session.key

    def _require_key(self, owner_id: str) -> bytes:
        key = self._live_key(owner_id)
        if key is None:
            raise SecretVaultLocked("the password vault is locked")
        return key

    def _seal(self, key: bytes, plaintext: str) -> str:
        """Vault key first, then the at-rest DEK — both locks are needed to read it back."""
        return self._vault.encrypt_str(_b64e(crypto.aead_encrypt(key, plaintext.encode())))

    def _open(self, key: bytes, token: str) -> str:
        return crypto.aead_decrypt(key, _b64d(self._vault.decrypt_str(token))).decode()

    def _view(self, key: bytes, row: SecretEntry) -> SecretEntryView:
        return SecretEntryView(
            id=row.id,
            name=self._open(key, row.name_enc),
            username=self._open(key, row.username_enc),
            url=self._open(key, row.url_enc),
            password=self._open(key, row.password_enc),
            created_at=row.created_at,
            updated_at=row.updated_at,
        )
