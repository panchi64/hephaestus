"""Outbound service credentials — the store behind the Service Keys section.

Holds the API keys the system calls *outbound* services with (today, the mail OAuth
clients), sealed with the vault exactly like the model-endpoint / search-provider
`api_key`. The set of services is a **static catalog** (`KNOWN_SERVICES`) — the system
declares which integrations it has; the store only holds a key per known service. A read
returns ``None`` while the vault is locked so callers degrade to their env fallback
rather than crashing at boot.

Raises domain errors only (`NotFoundError`); the route maps to HTTP.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import Engine
from sqlmodel import Session, select

from core.db import in_session
from core.exceptions import NotFoundError
from core.vault import Vault, VaultLocked
from models.service_credential import ServiceCredential

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ServiceInfo:
    """A declared outbound integration the operator can supply a key for."""

    id: str
    label: str
    purpose: str
    docs_url: str


# The outbound services the system integrates with. Display metadata lives here (not the
# DB); a key is stored per id. Extend this as new keyed integrations land.
#
# A service is declared here only while something reads its key. Three entries were
# removed when the surfaces that used them went (the model-quality rankings behind the
# Cookbook's recommendations, and the HuggingFace token that lifted the download rate
# limit) — a Service Keys row offering a credential nothing consumes is a settings
# surface lying about what the system does. Removing the declaration alone would have
# stranded any stored key: `status()` returns whatever rows exist, but the UI renders one
# row per *declared* service, so the operator could neither see it nor clear it. The rows
# are deleted with the declaration, by migration `b7ad41c9e208`.
KNOWN_SERVICES: tuple[ServiceInfo, ...] = (
    # The two OAuth *client* registrations mail uses (`EMAIL-1`). One per install, not
    # per mailbox — which is exactly what this static catalog models. The rotating
    # per-account token bundle is a different secret and lives sealed on the account row
    # (`MailAccount.secret_enc`), because this table cannot express several accounts,
    # refresh tokens, or expiry.
    ServiceInfo(
        "google_oauth",
        "Google (OAuth client)",
        (
            "Gmail accounts — the client secret of your Google OAuth app, used to obtain and "
            "refresh each connected mailbox's access."
        ),
        "https://console.cloud.google.com/apis/credentials",
    ),
    ServiceInfo(
        "microsoft_oauth",
        "Microsoft (OAuth client)",
        (
            "Outlook / Microsoft 365 accounts — the client secret of your Entra app "
            "registration, used to obtain and refresh each connected mailbox's access."
        ),
        "https://entra.microsoft.com/",
    ),
)
_SERVICE_IDS = frozenset(s.id for s in KNOWN_SERVICES)


class CredentialStore:
    def __init__(self, engine: Engine, vault: Vault) -> None:
        self._engine: Engine = engine
        self._vault: Vault = vault
        self._on_change: list[Callable[[], None]] = []

    @staticmethod
    def known_services() -> tuple[ServiceInfo, ...]:
        return KNOWN_SERVICES

    def on_change(self, callback: Callable[[], None]) -> None:
        """Register a callback fired after any credential write (set/clear) — so a
        consumer can invalidate what it cached under the old key, without a restart."""
        self._on_change.append(callback)

    def _fire_change(self) -> None:
        for callback in self._on_change:
            try:
                callback()
            except Exception:
                logger.warning("credentials: on_change callback failed", exc_info=True)

    async def status(self, owner_id: str) -> dict[str, bool]:
        """``service id`` → whether a key is stored (no decryption — just presence)."""

        def work(session: Session) -> dict[str, bool]:
            rows = session.exec(
                select(ServiceCredential).where(ServiceCredential.owner_id == owner_id)
            ).all()
            return {row.service: row.api_key_enc is not None for row in rows}

        return await in_session(self._engine, work)

    async def set_key(self, owner_id: str, service: str, api_key: str) -> None:
        """Seal and upsert the key for a known service. An empty key clears it."""
        if service not in _SERVICE_IDS:
            raise NotFoundError(f"unknown service {service!r}")
        if not api_key:
            await self.clear_key(owner_id, service)
            return
        enc = self._vault.encrypt_str(api_key)

        def work(session: Session) -> None:
            row = session.exec(
                select(ServiceCredential).where(
                    ServiceCredential.owner_id == owner_id,
                    ServiceCredential.service == service,
                )
            ).first()
            if row is None:
                row = ServiceCredential(owner_id=owner_id, service=service, api_key_enc=enc)
            else:
                row.api_key_enc = enc
                row.updated_at = datetime.now(UTC)
            session.add(row)
            session.flush()

        await in_session(self._engine, work)
        self._fire_change()

    async def clear_key(self, owner_id: str, service: str) -> None:
        if service not in _SERVICE_IDS:
            raise NotFoundError(f"unknown service {service!r}")

        def work(session: Session) -> None:
            row = session.exec(
                select(ServiceCredential).where(
                    ServiceCredential.owner_id == owner_id,
                    ServiceCredential.service == service,
                )
            ).first()
            if row is not None:
                session.delete(row)

        await in_session(self._engine, work)
        self._fire_change()

    async def get_secret(self, owner_id: str, service: str) -> str | None:
        """The decrypted key for a service, or ``None`` when unset **or the vault is
        locked** — so consumers degrade to their env fallback at boot instead of
        raising."""
        if not self._vault.is_unlocked:
            return None

        def work(session: Session) -> str | None:
            row = session.exec(
                select(ServiceCredential).where(
                    ServiceCredential.owner_id == owner_id,
                    ServiceCredential.service == service,
                )
            ).first()
            return row.api_key_enc if row is not None else None

        enc = await in_session(self._engine, work)
        if not enc:
            return None
        try:
            return self._vault.decrypt_str(enc)
        except VaultLocked:
            return None
