"""What a contribution type is: one kind of thing a plugin can add to the platform.

A plugin is a name, metadata and its contributions. Every kind of contribution is one
:class:`ContributionType` registered in :mod:`.registry`, so a new kind of plugin is one new
module here (plus a frontend renderer if it shows UI); the parser, store, loader and Plugins
screen never name a kind.

Two tiers declare contributions. A **sandboxed** plugin (one the model writes, run in a VM)
declares them in ``plugin.yaml`` and its files, which :meth:`ContributionType.parse` reads. A
**built-in** plugin (``backend/plugins/<name>/``) builds them as Python objects at boot. A
type says which tiers may use it, and whether it can act on anything (``privileged``) or only
show something to the operator (``inert``): a sandboxed plugin whose every contribution is
inert goes live without the operator publishing it.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Literal, override

from core.exceptions import PluginValidationError

type Tier = Literal["builtin", "sandboxed"]
type Trust = Literal["inert", "privileged"]
type Described = dict[str, str | int | bool | list[str] | None]

BOTH_TIERS: frozenset[Tier] = frozenset({"builtin", "sandboxed"})
BUILTIN_ONLY: frozenset[Tier] = frozenset({"builtin"})


@dataclass(frozen=True)
class BundleSource:
    """What a sandboxed plugin's contribution is parsed from."""

    plugin_name: str
    #: The value under this type's key in ``plugin.yaml``.
    raw: object
    #: Whether ``plugin.yaml`` has this type's key at all.
    declared: bool
    #: The bundle's files by path; None when only the manifest text is being checked.
    files: Mapping[str, bytes] | None


class ContributionType:
    """One kind of contribution, as the registry and loader see it: items are opaque."""

    def __init__(
        self,
        key: str,
        label: str,
        *,
        trust: Trust = "privileged",
        tiers: frozenset[Tier] = BOTH_TIERS,
    ) -> None:
        #: The ``plugin.yaml`` key, and the name the Plugins screen groups items under.
        self.key: str = key
        #: A human label for the group, e.g. "Windows".
        self.label: str = label
        self.trust: Trust = trust
        self.tiers: frozenset[Tier] = tiers

    def parse(self, source: BundleSource) -> tuple[object, ...]:
        """The items a sandboxed plugin declares. Raises ``PluginValidationError``."""
        if source.declared:
            raise PluginValidationError(self.key, f"only built-in plugins contribute {self.key}")
        return ()

    def describe(self, item: object) -> Described:
        """One item as the Plugins screen lists it; rendered generically."""
        return {"name": str(getattr(item, "name", self.key))}

    def owns(self, item: object) -> bool:
        """Whether a built-in plugin's item is one of this type's."""
        del item
        return False

    async def activate(self, plugin: str, items: tuple[object, ...]) -> None:
        """Bring a built-in plugin's items to life when it is switched on."""
        del plugin, items

    async def deactivate(self, plugin: str, items: tuple[object, ...]) -> None:
        """Stand them down when it is switched off or the app stops."""
        del plugin, items


class TypedContribution[T](ContributionType):
    """A contribution type whose items are instances of ``item_type``; override the typed
    hooks (``describe_one``, ``start``, ``stop``) rather than the opaque ones."""

    def __init__(
        self,
        key: str,
        label: str,
        item_type: type[T],
        *,
        trust: Trust = "privileged",
        tiers: frozenset[Tier] = BOTH_TIERS,
    ) -> None:
        super().__init__(key, label, trust=trust, tiers=tiers)
        self.item_type: type[T] = item_type

    @override
    def owns(self, item: object) -> bool:
        return isinstance(item, self.item_type)

    def items(self, found: Iterable[object]) -> tuple[T, ...]:
        return tuple(item for item in found if isinstance(item, self.item_type))

    def of(self, contributions: Mapping[str, tuple[object, ...]]) -> tuple[T, ...]:
        return self.items(contributions.get(self.key, ()))

    @override
    def describe(self, item: object) -> Described:
        if isinstance(item, self.item_type):
            return self.describe_one(item)
        return super().describe(item)

    def describe_one(self, item: T) -> Described:
        return super().describe(item)

    @override
    async def activate(self, plugin: str, items: tuple[object, ...]) -> None:
        await self.start(plugin, self.items(items))

    @override
    async def deactivate(self, plugin: str, items: tuple[object, ...]) -> None:
        await self.stop(plugin, self.items(items))

    async def start(self, plugin: str, items: tuple[T, ...]) -> None:
        """Typed :meth:`activate`."""
        del plugin, items

    async def stop(self, plugin: str, items: tuple[T, ...]) -> None:
        """Typed :meth:`deactivate`."""
        del plugin, items
