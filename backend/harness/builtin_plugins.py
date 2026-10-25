"""Built-in plugins switched on and off while the app runs.

The switch is read on every request and turn, so it is cached in memory and written
through to the settings store. Switching a plugin off deactivates its contributions and
gates its surfaces (routes, tools, prompt blocks) without unbuilding its services: a run of
its kind parked before the switch must still be able to resume.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from fastapi import HTTPException, Request
from pydantic_ai import RunContext

from core.container import ServiceContainer
from harness.manifest import FeatureManifest, PluginInfo
from routes.deps import OPERATOR_ID, service
from services.plugins.contributions.base import ContributionType, Described
from services.plugins.contributions.registry import load_types
from services.settings_store import SettingsStore
from services.tool_policy import AvailabilityCheck
from tools import InstructionProvider, PromptContextProvider, RunDeps
from tools.deps import PromptContextRequest

logger = logging.getLogger(__name__)


def _key(name: str) -> str:
    return f"plugins.builtin.{name}.enabled"


@dataclass(frozen=True)
class BuiltinPluginView:
    name: str
    title: str
    description: str
    enabled: bool
    contributions: dict[str, list[Described]]


class BuiltinPlugins:
    def __init__(self, settings: SettingsStore, manifests: Sequence[FeatureManifest]) -> None:
        self._settings: SettingsStore = settings
        self._info: dict[str, PluginInfo] = {
            m.name: m.plugin for m in manifests if m.plugin is not None
        }
        self._enabled: dict[str, bool] = {
            name: info.default_enabled for name, info in self._info.items()
        }
        self._items: dict[str, tuple[object, ...]] = {}
        self._active: set[str] = set()
        self._locks: dict[str, asyncio.Lock] = {name: asyncio.Lock() for name in self._info}

    async def load(self) -> None:
        """Read the stored switches; a plugin never switched keeps its default."""
        for name, info in self._info.items():
            default = "true" if info.default_enabled else "false"
            stored = await self._settings.get(OPERATOR_ID, _key(name), default)
            self._enabled[name] = stored == "true"

    def is_plugin(self, name: str) -> bool:
        return name in self._info

    def enabled(self, name: str) -> bool:
        """Whether a built-in plugin is on; a name that is no built-in plugin always is."""
        return self._enabled.get(name, True)

    async def attach(self, name: str, items: tuple[object, ...]) -> None:
        """Hold a freshly built plugin's contributions, activating them if it is on."""
        self._items[name] = items
        if self.enabled(name):
            await self._activate(name)

    async def set_enabled(self, name: str, enabled: bool) -> None:
        if name not in self._info:
            raise KeyError(name)
        async with self._locks[name]:
            # On only once everything started: a failed start leaves it off, and stored so.
            if enabled:
                await self._activate(name)
            self._enabled[name] = enabled
            await self._settings.set(OPERATOR_ID, _key(name), "true" if enabled else "false")
            if not enabled:
                await self.deactivate(name)

    async def deactivate(self, name: str) -> None:
        if name not in self._active:
            return
        self._active.discard(name)
        for kind, items in reversed(self._grouped(name)):
            await kind.deactivate(name, items)

    async def _activate(self, name: str) -> None:
        if name in self._active or name not in self._items:
            return
        started: list[tuple[ContributionType, tuple[object, ...]]] = []
        try:
            for kind, items in self._grouped(name):
                await kind.activate(name, items)
                started.append((kind, items))
        except Exception:
            # What already started is stopped again, or it would run with no way to stop it.
            for kind, items in reversed(started):
                try:
                    await kind.deactivate(name, items)
                except Exception:
                    logger.exception("plugins: stopping %s's %s failed", name, kind.key)
            raise
        self._active.add(name)

    def _grouped(self, name: str) -> list[tuple[ContributionType, tuple[object, ...]]]:
        registry = load_types()
        grouped: dict[str, list[object]] = {}
        for item in self._items.get(name, ()):
            grouped.setdefault(registry.kind_of(item).key, []).append(item)
        return [(registry.kind_of(found[0]), tuple(found)) for found in grouped.values()]

    def views(self) -> list[BuiltinPluginView]:
        registry = load_types()
        views: list[BuiltinPluginView] = []
        for name, info in sorted(self._info.items()):
            described: dict[str, list[Described]] = {}
            for item in self._items.get(name, ()):
                kind = registry.kind_of(item)
                described.setdefault(kind.key, []).append(kind.describe(item))
            views.append(
                BuiltinPluginView(name, info.title, info.description, self.enabled(name), described)
            )
        return views


def plugin_gate(name: str) -> Callable[[Request], None]:
    """A route dependency answering 404 while the plugin ``name`` is switched off."""

    def gate(request: Request) -> None:
        if not service(request, BuiltinPlugins).enabled(name):
            raise HTTPException(status_code=404, detail=f"the {name} plugin is switched off")

    return gate


def _switched_on(caps: ServiceContainer, name: str) -> bool:
    plugins = caps.get_optional(BuiltinPlugins)
    return plugins is None or plugins.enabled(name)


def gate_availability(name: str, check: AvailabilityCheck | None) -> AvailabilityCheck:
    async def gated(caps: ServiceContainer, owner_id: str) -> bool:
        if not _switched_on(caps, name):
            return False
        return check is None or await check(caps, owner_id)

    return gated


def gate_instruction(name: str, provider: InstructionProvider) -> InstructionProvider:
    async def gated(ctx: RunContext[RunDeps]) -> str:
        if not _switched_on(ctx.deps.caps, name):
            return ""
        result = provider(ctx)
        return result if isinstance(result, str) else await result

    return gated


def gate_prompt_context(name: str, provider: PromptContextProvider) -> PromptContextProvider:
    async def gated(request: PromptContextRequest) -> str:
        if not _switched_on(request.caps, name):
            return ""
        return await provider(request)

    return gated
