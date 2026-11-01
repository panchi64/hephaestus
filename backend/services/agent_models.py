"""The operator's model settings for agents, beyond the role bindings.

Two lists in the settings store: a model pinned on each agent that has no editable row of
its own (built-in, project and plugin agents, keyed by their ``<source>:<name>`` id), and
the models Talos may choose from when it spawns an agent with no pin. A stored agent's pin
lives on its row.
"""

from __future__ import annotations

import asyncio
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from services.settings_store import SettingsStore

PINS_KEY = "agents.model_pins"
CHOOSABLE_KEY = "agents.choosable_models"

#: The longest note the operator may leave on a choosable model.
NOTE_MAX_CHARS = 200


class _Value(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)


class ModelPin(_Value):
    """An endpoint and a model on it. A null model is the endpoint's default."""

    endpoint_id: str | None = None
    model: str | None = None

    @property
    def empty(self) -> bool:
        return self.endpoint_id is None and self.model is None


class ChoosableModel(_Value):
    endpoint_id: str
    model: str
    #: What the operator means it for, in their words; shown to Talos verbatim.
    note: str = Field(default="", max_length=NOTE_MAX_CHARS)


_PINS: TypeAdapter[dict[str, ModelPin]] = TypeAdapter(dict[str, ModelPin])
_CHOOSABLE: TypeAdapter[list[ChoosableModel]] = TypeAdapter(list[ChoosableModel])


class AgentModels:
    def __init__(self, settings: SettingsStore) -> None:
        self._settings: SettingsStore = settings
        #: Each write reads the whole value back first, so two at once would lose one.
        self._writing: asyncio.Lock = asyncio.Lock()

    async def pins(self, owner_id: str) -> dict[str, ModelPin]:
        """Pins by agent id. A corrupt value reads as none."""
        raw = await self._settings.get(owner_id, PINS_KEY)
        if not raw:
            return {}
        try:
            return _PINS.validate_json(raw)
        except ValidationError:
            return {}

    async def set_pin(self, owner_id: str, agent_id: str, pin: ModelPin) -> None:
        """Pin ``agent_id``, or clear its pin when ``pin`` is empty."""
        async with self._writing:
            pins = await self.pins(owner_id)
            if pin.empty:
                _ = pins.pop(agent_id, None)
            else:
                pins[agent_id] = pin
            await self._settings.set(owner_id, PINS_KEY, _PINS.dump_json(pins).decode())

    async def choosable(self, owner_id: str) -> list[ChoosableModel]:
        """The list as the operator saved it, unavailable entries included. Corrupt reads as
        empty, which offers no choice."""
        raw = await self._settings.get(owner_id, CHOOSABLE_KEY)
        if not raw:
            return []
        try:
            return _CHOOSABLE.validate_json(raw)
        except ValidationError:
            return []

    async def set_choosable(self, owner_id: str, entries: list[ChoosableModel]) -> None:
        """Replace the list. Duplicates of one endpoint and model keep the first."""
        seen: set[tuple[str, str]] = set()
        kept: list[ChoosableModel] = []
        for entry in entries:
            key = (entry.endpoint_id, entry.model)
            if key not in seen:
                seen.add(key)
                kept.append(entry)
        async with self._writing:
            await self._settings.set(owner_id, CHOOSABLE_KEY, _CHOOSABLE.dump_json(kept).decode())
