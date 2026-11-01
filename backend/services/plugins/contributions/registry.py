"""Every contribution type, by key. A module in this package that defines a type registers
it here at import; :func:`load_types` imports them all, so presence is registration."""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Iterator
from pathlib import Path

from .base import ContributionType

_PACKAGE = __name__.rpartition(".")[0]
_INFRASTRUCTURE = frozenset({"base", "registry"})


class ContributionRegistry:
    def __init__(self) -> None:
        self._types: dict[str, ContributionType] = {}

    def register[C: ContributionType](self, kind: C) -> C:
        existing = self._types.get(kind.key)
        if existing is not None and type(existing) is not type(kind):
            raise ValueError(f"contribution type {kind.key!r} is registered twice")
        self._types[kind.key] = kind
        return kind

    def get(self, key: str) -> ContributionType | None:
        return self._types.get(key)

    def keys(self) -> list[str]:
        return sorted(self._types)

    def kind_of(self, item: object) -> ContributionType:
        """The type a built-in plugin's item belongs to, by its class."""
        for kind in self._types.values():
            if kind.owns(item):
                return kind
        raise TypeError(f"{type(item).__name__} is no registered contribution type's item")

    def __iter__(self) -> Iterator[ContributionType]:
        return iter([self._types[key] for key in self.keys()])


CONTRIBUTIONS = ContributionRegistry()


def load_types() -> ContributionRegistry:
    """Import every contribution module in this package and return the registry."""
    for info in pkgutil.iter_modules([str(Path(__file__).parent)]):
        if info.name not in _INFRASTRUCTURE:
            _ = importlib.import_module(f"{_PACKAGE}.{info.name}")
    return CONTRIBUTIONS
