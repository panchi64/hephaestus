"""What goes in a backup — discovered from the models, never listed here (`BACKUP-1`).

An entity declares its own membership with a ``__backup__`` marker (``models/_backup.py``).
This module walks the ``models`` package, collects the marked tables off SQLModel's mapper
registry, and orders them for import. The consequence that matters: **adding a table to the
backup is a one-line edit in that table's own file**, so features landing in parallel never
contend for a central list, and a table nobody marked is simply never exported.

The walk imports every ``models.*`` module for the same reason ``migrations/env.py`` does —
a class the process never imported has no mapper, and would silently vanish from exports.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from sqlalchemy import Table
from sqlmodel import SQLModel

from models._backup import BackupSpec
from models._discovery import import_all_models


class BackupModel(SQLModel):
    """The shape every backed-up table shares, for typing only.

    No table inherits it, so ``isinstance``/``issubclass`` against it is always False.
    """

    __table__: ClassVar[Table]
    id: str
    owner_id: str


@dataclass(frozen=True)
class BackupEntity:
    """One marked table, paired with what its marker declared."""

    model: type[BackupModel]
    spec: BackupSpec

    @property
    def name(self) -> str:
        """The class name — how rows are keyed inside an export file, so the importer can
        find the table again without depending on section layout."""
        return self.model.__name__


def discover_entities() -> list[BackupEntity]:
    """Every table that marked itself for backup, in import order: section, then the
    marker's ``order`` (parents before children), then name for a stable tie-break."""
    import_all_models()
    found: dict[str, BackupEntity] = {}
    for mapper in SQLModel._sa_registry.mappers:
        cls = mapper.class_
        # `__dict__`, not `getattr`: a subclass must declare its own membership rather
        # than inherit a parent's section by accident.
        spec = cls.__dict__.get("__backup__")
        if isinstance(spec, BackupSpec):
            found[cls.__name__] = BackupEntity(model=cls, spec=spec)
    return sorted(found.values(), key=lambda e: (e.spec.section, e.spec.order, e.name))


def sections() -> tuple[str, ...]:
    """The operator-facing groups that actually have something behind them, in order.
    A category with no marked entity is simply absent — never fabricated."""
    seen: list[str] = []
    for entity in discover_entities():
        if entity.spec.section not in seen:
            seen.append(entity.spec.section)
    return tuple(seen)
