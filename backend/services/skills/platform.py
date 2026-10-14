"""The platform skills an owner has switched on.

Shipped skills are on unless the operator turned one off; the names turned off are kept
as one setting, so a skill added in a later release starts on.
"""

from __future__ import annotations

import json
from collections.abc import Callable

from core.exceptions import NotFoundError
from core.serde import parse_json
from services.settings_store import SettingsStore

from .sources import DiskSkill, platform_skills

DISABLED_KEY = "skills.platform_disabled"


class PlatformSkills:
    def __init__(
        self,
        settings: SettingsStore,
        skills: Callable[[], tuple[DiskSkill, ...]] = platform_skills,
    ) -> None:
        self._settings: SettingsStore = settings
        self._skills: Callable[[], tuple[DiskSkill, ...]] = skills

    def all(self) -> tuple[DiskSkill, ...]:
        return self._skills()

    async def disabled(self, owner_id: str) -> frozenset[str]:
        raw = await self._settings.get(owner_id, DISABLED_KEY)
        try:
            names = parse_json(raw) if raw else []
        except ValueError:
            return frozenset()
        if not isinstance(names, list):
            return frozenset()
        return frozenset(name for name in names if isinstance(name, str))

    async def enabled(self, owner_id: str) -> tuple[DiskSkill, ...]:
        off = await self.disabled(owner_id)
        return tuple(entry for entry in self._skills() if entry.skill.name not in off)

    async def set_enabled(self, owner_id: str, name: str, enabled: bool) -> None:
        if all(entry.skill.name != name for entry in self._skills()):
            raise NotFoundError(f"No platform skill named {name!r}")
        off = set(await self.disabled(owner_id))
        if enabled:
            off.discard(name)
        else:
            off.add(name)
        await self._settings.set(owner_id, DISABLED_KEY, json.dumps(sorted(off)))
