"""The Discord settings kept beside the token: which guild channels may talk to the agent,
and where unlinked approvals go."""

from __future__ import annotations

import json

from core.serde import parse_json
from routes.deps import OPERATOR_ID
from services.settings_store import SettingsStore

_CHANNELS_KEY = "discord.allowed_channels"
_TO_DM_KEY = "discord.approvals_to_dm"


def is_snowflake(value: str) -> bool:
    return value.isascii() and value.isdigit()


class DiscordConfig:
    def __init__(self, settings: SettingsStore) -> None:
        self._settings: SettingsStore = settings

    async def allowed_channels(self) -> list[str]:
        raw = await self._settings.get(OPERATOR_ID, _CHANNELS_KEY)
        try:
            stored = parse_json(raw) if raw else []
        except ValueError:
            return []
        return [s for s in stored if isinstance(s, str)] if isinstance(stored, list) else []

    async def set_allowed_channels(self, channels: list[str]) -> None:
        await self._settings.set(OPERATOR_ID, _CHANNELS_KEY, json.dumps(sorted(set(channels))))

    async def approvals_to_dm(self) -> bool:
        return await self._settings.get(OPERATOR_ID, _TO_DM_KEY) == "true"

    async def set_approvals_to_dm(self, enabled: bool) -> None:
        await self._settings.set(OPERATOR_ID, _TO_DM_KEY, "true" if enabled else "false")
