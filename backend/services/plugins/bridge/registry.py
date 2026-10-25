"""Every bridge capability, by method. A module in this package that defines capabilities
registers them here at import; :func:`load_capabilities` imports them all."""

from __future__ import annotations

import importlib
import pkgutil
from pathlib import Path

from ..contributions.permissions import BRIDGE_PERMISSIONS
from .base import BridgeCapability

_PACKAGE = __name__.rpartition(".")[0]
_INFRASTRUCTURE = frozenset({"base", "registry", "gate"})


class BridgeRegistry:
    def __init__(self) -> None:
        self._methods: dict[str, BridgeCapability] = {}

    def register(self, *capabilities: BridgeCapability) -> None:
        for capability in capabilities:
            existing = self._methods.get(capability.method)
            if existing is not None and existing is not capability:
                raise ValueError(f"bridge method {capability.method!r} is registered twice")
            self._methods[capability.method] = capability
            BRIDGE_PERMISSIONS.declare(frozenset({capability.permission}))

    def get(self, method: str) -> BridgeCapability | None:
        return self._methods.get(method)

    def methods(self) -> list[str]:
        return sorted(self._methods)


BRIDGE = BridgeRegistry()


def load_capabilities() -> BridgeRegistry:
    """Import every capability module in this package and return the registry."""
    for info in pkgutil.iter_modules([str(Path(__file__).parent)]):
        if info.name not in _INFRASTRUCTURE:
            _ = importlib.import_module(f"{_PACKAGE}.{info.name}")
    return BRIDGE
