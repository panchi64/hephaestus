"""The operator's secret values for plugins, sealed in the vault and bound to a host."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import Engine, delete
from sqlmodel import Session, col, select

from core.db import in_session
from core.exceptions import PluginValidationError
from core.vault import Vault
from models.plugin import PluginSecret

from .manifest import secret_domain
from .store import PluginStore


@dataclass(frozen=True)
class SecretStatus:
    name: str
    domain: str
    is_set: bool


@dataclass(frozen=True)
class SecretValue:
    value: str
    domain: str


class PluginSecretStore:
    def __init__(self, engine: Engine, vault: Vault, plugins: PluginStore) -> None:
        self._engine: Engine = engine
        self._vault: Vault = vault
        self._plugins: PluginStore = plugins

    async def secrets(self, owner_id: str, plugin_id: str) -> list[SecretStatus]:
        """The secrets the current version declares, each with the host its value goes
        to and whether the operator has set it."""
        view = await self._plugins.get(owner_id, plugin_id)
        stored = await self._stored_secrets(owner_id, plugin_id)
        return [
            SecretStatus(name, stored[name].domain if name in stored else domain, name in stored)
            for name, domain in sorted(view.current.secrets.items())
        ]

    async def secret_values(self, owner_id: str, plugin_id: str) -> dict[str, SecretValue]:
        return await self._stored_secrets(owner_id, plugin_id)

    async def set_secret(
        self, owner_id: str, plugin_id: str, name: str, value: str, domain: str | None = None
    ) -> None:
        """Store a secret's value, bound to ``domain`` (by default the host the manifest
        names for it). Only the operator calls this."""
        view = await self._plugins.get(owner_id, plugin_id)
        declared = view.current.secrets
        if name not in declared:
            raise PluginValidationError("secrets", f"{view.name} declares no secret {name!r}")
        host = declared[name] if domain is None else secret_domain(domain)
        if not value:
            raise PluginValidationError("secrets", "a secret needs a value")

        def work(session: Session) -> None:
            row = session.exec(
                select(PluginSecret)
                .where(PluginSecret.plugin_id == plugin_id)
                .where(PluginSecret.name == name)
            ).first()
            encrypted = self._vault.encrypt_str(value)
            if row is None:
                row = PluginSecret(
                    owner_id=owner_id,
                    plugin_id=plugin_id,
                    name=name,
                    domain=host,
                    value_enc=encrypted,
                )
            row.domain = host
            row.value_enc = encrypted
            row.updated_at = datetime.now(UTC)
            session.add(row)

        await in_session(self._engine, work)

    async def delete_secret(self, owner_id: str, plugin_id: str, name: str) -> None:
        _ = await self._plugins.get(owner_id, plugin_id)

        def work(session: Session) -> None:
            _ = session.exec(
                delete(PluginSecret)
                .where(col(PluginSecret.plugin_id) == plugin_id)
                .where(col(PluginSecret.name) == name)
            )

        await in_session(self._engine, work)

    async def _stored_secrets(self, owner_id: str, plugin_id: str) -> dict[str, SecretValue]:
        def work(session: Session) -> dict[str, SecretValue]:
            rows = session.exec(
                select(PluginSecret)
                .where(PluginSecret.owner_id == owner_id)
                .where(PluginSecret.plugin_id == plugin_id)
            ).all()
            return {
                row.name: SecretValue(self._vault.decrypt_str(row.value_enc), row.domain)
                for row in rows
            }

        return await in_session(self._engine, work)
