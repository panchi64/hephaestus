"""Process-wide tables that core seeds and feature manifests extend at discovery.

A module below ``harness/`` that has to answer for every feature (a tool's sensitivity, a
run kind's lane) can't import the features, so each feature declares its rows here instead.
Discovery runs once per app and again in tests, so declaring an identical row twice is a
no-op; a conflicting row raises, since two features disagreeing about one name is a bug.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping


class DeclarationConflictError(ValueError):
    """Two declarations disagree about one key."""


class DeclaredTable[K, V]:
    """A mapping seeded by core, extended by ``declare`` and never shrunk."""

    def __init__(self, name: str, seed: Mapping[K, V] | None = None) -> None:
        self._name: str = name
        self._rows: dict[K, V] = dict(seed or {})

    def declare(self, rows: Mapping[K, V]) -> None:
        for key, value in rows.items():
            existing = self._rows.get(key)
            if key in self._rows and existing != value:
                raise DeclarationConflictError(
                    f"{self._name}: {key!r} is already declared as {existing!r}, not {value!r}"
                )
            self._rows[key] = value

    def get(self, key: K) -> V | None:
        return self._rows.get(key)

    def __getitem__(self, key: K) -> V:
        return self._rows[key]

    def __contains__(self, key: object) -> bool:
        return key in self._rows

    def __iter__(self) -> Iterator[K]:
        return iter(self._rows)

    def __len__(self) -> int:
        return len(self._rows)

    def items(self) -> list[tuple[K, V]]:
        return list(self._rows.items())


class DeclaredSet[T]:
    """A set seeded by core and extended by ``declare``; additions are idempotent."""

    def __init__(self, seed: Iterable[T] = ()) -> None:
        self._items: set[T] = set(seed)

    def declare(self, items: frozenset[T]) -> None:
        self._items |= items

    def __contains__(self, item: object) -> bool:
        return item in self._items

    def snapshot(self) -> frozenset[T]:
        return frozenset(self._items)
