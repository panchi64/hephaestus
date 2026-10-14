"""Import every ``models.*`` module so its tables register on SQLModel metadata.

A class the process never imported has no mapper: Alembic would silently omit its
table from autogenerate/upgrade, and the backup walk would silently skip exporting
it. Both consumers call this instead of keeping a hand-list of imports that has to
be edited every time an entity lands — the module's presence in the package *is*
its registration.
"""

from __future__ import annotations

import importlib
import pkgutil

import models


def import_all_models() -> None:
    for info in pkgutil.iter_modules(models.__path__):
        if not info.name.startswith("_"):
            _ = importlib.import_module(f"{models.__name__}.{info.name}")
