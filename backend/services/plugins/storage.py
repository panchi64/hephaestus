"""What a plugin's windows keep between openings: small JSON values by key, per plugin.

Values are sealed with the vault. A plugin sees only its own keys, and the quotas keep a
window's code from filling the database. Rows go with the plugin when it is deleted, and
are not part of its exported bundle.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from pydantic import JsonValue
from sqlalchemy import Engine, delete, func
from sqlmodel import Session, col, select

from core.db import in_session
from core.exceptions import InvalidInputError
from core.serde import parse_json
from core.vault import Vault
from models.plugin import PluginStorageEntry

KEY_MAX_CHARS = 128
VALUE_MAX_BYTES = 64 * 1024
MAX_KEYS = 500
MAX_BYTES = 1024 * 1024


class PluginStorage:
    def __init__(self, engine: Engine, vault: Vault) -> None:
        self._engine: Engine = engine
        self._vault: Vault = vault

    async def get(self, owner_id: str, plugin_id: str, key: str) -> JsonValue:
        """The value under ``key``, or None when there is none."""

        def work(session: Session) -> JsonValue:
            row = _find(session, owner_id, plugin_id, _key(key))
            if row is None:
                return None
            return parse_json(self._vault.decrypt_str(row.value_enc))

        return await in_session(self._engine, work)

    async def set(self, owner_id: str, plugin_id: str, key: str, value: JsonValue) -> None:
        clean = _key(key)
        text = json.dumps(value, separators=(",", ":"))
        size = len(text.encode())
        if size > VALUE_MAX_BYTES:
            raise InvalidInputError(f"a stored value is at most {VALUE_MAX_BYTES // 1024} KB")

        def work(session: Session) -> None:
            row = _find(session, owner_id, plugin_id, clean)
            count, total = session.exec(
                select(func.count(), func.coalesce(func.sum(PluginStorageEntry.size_bytes), 0))
                .where(PluginStorageEntry.owner_id == owner_id)
                .where(PluginStorageEntry.plugin_id == plugin_id)
            ).one()
            if row is None and count >= MAX_KEYS:
                raise InvalidInputError(f"a plugin stores at most {MAX_KEYS} keys")
            if total - (0 if row is None else row.size_bytes) + size > MAX_BYTES:
                raise InvalidInputError(f"a plugin stores at most {MAX_BYTES // 1024} KB")
            if row is None:
                row = PluginStorageEntry(
                    owner_id=owner_id, plugin_id=plugin_id, key=clean, value_enc="", size_bytes=0
                )
            row.value_enc = self._vault.encrypt_str(text)
            row.size_bytes = size
            row.updated_at = datetime.now(UTC)
            session.add(row)

        await in_session(self._engine, work)

    async def delete(self, owner_id: str, plugin_id: str, key: str) -> None:
        clean = _key(key)

        def work(session: Session) -> None:
            _ = session.exec(
                delete(PluginStorageEntry)
                .where(col(PluginStorageEntry.owner_id) == owner_id)
                .where(col(PluginStorageEntry.plugin_id) == plugin_id)
                .where(col(PluginStorageEntry.key) == clean)
            )

        await in_session(self._engine, work)

    async def keys(self, owner_id: str, plugin_id: str) -> list[str]:
        def work(session: Session) -> list[str]:
            return list(
                session.exec(
                    select(PluginStorageEntry.key)
                    .where(PluginStorageEntry.owner_id == owner_id)
                    .where(PluginStorageEntry.plugin_id == plugin_id)
                    .order_by(col(PluginStorageEntry.key))
                ).all()
            )

        return await in_session(self._engine, work)


def _key(raw: str) -> str:
    if not raw or len(raw) > KEY_MAX_CHARS:
        raise InvalidInputError(f"a storage key is 1 to {KEY_MAX_CHARS} characters")
    return raw


def _find(session: Session, owner_id: str, plugin_id: str, key: str) -> PluginStorageEntry | None:
    return session.exec(
        select(PluginStorageEntry)
        .where(PluginStorageEntry.owner_id == owner_id)
        .where(PluginStorageEntry.plugin_id == plugin_id)
        .where(PluginStorageEntry.key == key)
    ).first()
