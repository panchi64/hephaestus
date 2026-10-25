"""Outbound service credentials — the store behind the Service Keys section.

Holds the API keys the system calls *outbound* services with, sealed with the vault exactly
like the model-endpoint / search-provider `api_key`. The set of services is a **declared
catalog**: each feature declares the integrations it reads a key for
(``FeatureManifest.credentials``); the store only holds a key per declared service. A read
returns ``None`` while the vault is locked so callers degrade to their env fallback
rather than crashing at boot.

Raises domain errors only (`NotFoundError`); the route maps to HTTP.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import Engine
from sqlmodel import Session, select

from core.db import in_session
from core.declarations import DeclaredTable
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


# Declared only while something reads the key: the UI renders one row per declared
# service, so a declaration with no consumer is a settings row lying about the system, and
# a stored key whose declaration is gone can't be seen or cleared (retired rows are deleted
# by migration, as `b7ad41c9e208` did).
_SERVICES: DeclaredTable[str, ServiceInfo] = DeclaredTable("outbound services")


def declare_services(services: Iterable[ServiceInfo]) -> None:
    _SERVICES.declare({service.id: service for service in services})


def known_services() -> tuple[ServiceInfo, ...]:
    return tuple(_SERVICES[service_id] for service_id in _SERVICES)


class CredentialStore:
    def __init__(self, engine: Engine, vault: Vault) -> None:
        self._engine: Engine = engine
        self._vault: Vault = vault
        self._on_change: list[Callable[[], None]] = []

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
        if service not in _SERVICES:
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
        if service not in _SERVICES:
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
