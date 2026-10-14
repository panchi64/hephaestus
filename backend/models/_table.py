"""The base every table model inherits instead of ``SQLModel``."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from sqlalchemy import UniqueConstraint
from sqlmodel import SQLModel

type TableArgs = tuple[UniqueConstraint, ...]


class Table(SQLModel):
    """A table model's base, so each model can name its table with a plain string."""

    if TYPE_CHECKING:
        # SQLModel 0.0.48 declares __tablename__ as a declared_attr method, so assigning a
        # string to it is a type error.
        __tablename__: ClassVar[str]  # pyright: ignore[reportIncompatibleVariableOverride]
