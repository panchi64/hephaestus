"""``permissions``: what a plugin's windows may reach through the bridge.

```yaml
permissions: [storage, chat.read]
```

A window can call only the bridge methods whose permission its plugin lists. Each bridge
capability declares the permission it needs into :data:`BRIDGE_PERMISSIONS`, so a new
capability adds its own name.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import override

from core.declarations import DeclaredSet
from core.exceptions import PluginValidationError

from ..fields import as_list
from .base import BundleSource, Described, TypedContribution
from .registry import CONTRIBUTIONS

BRIDGE_PERMISSIONS: DeclaredSet[str] = DeclaredSet({"storage", "tools", "chat.read", "chat.post"})


@dataclass(frozen=True)
class BridgePermission:
    name: str


class Permissions(TypedContribution[BridgePermission]):
    @override
    def parse(self, source: BundleSource) -> tuple[BridgePermission, ...]:
        names = [str(raw) for raw in as_list(source.raw, self.key)]
        known = BRIDGE_PERMISSIONS.snapshot()
        for name in names:
            if name not in known:
                raise PluginValidationError(
                    self.key, f"{name!r} is not a permission; use {', '.join(sorted(known))}"
                )
        return tuple(BridgePermission(name) for name in sorted(set(names)))

    @override
    def describe_one(self, item: BridgePermission) -> Described:
        return {"name": item.name}


PERMISSIONS = CONTRIBUTIONS.register(
    Permissions("permissions", "Window permissions", BridgePermission, trust="inert")
)
