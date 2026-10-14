"""The backup marker — how an entity declares that it belongs in an export (`BACKUP-*`).

A table opts *itself* into backup by carrying a ``__backup__`` class attribute, exactly as it
carries ``__tablename__``:

.. code-block:: python

    class Memory(Table, table=True):
        __tablename__ = "memories"
        __backup__ = BackupSpec(section="memories", natural_key=("content_enc",))

``services/backup`` discovers markers by walking this package, so **there is no central list
to edit** — a new entity ships its own membership, and two features adding tables in parallel
never touch the same line. The dunder name matters: SQLAlchemy's declarative machinery leaves
those alone, so the marker is inert as far as the ORM is concerned.

Values here are declarations, never behavior. What "seal this column" or "skip a duplicate"
*means* lives in the service; this file only says which columns and which section.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BackupSpec:
    """One entity's place in an export.

    ``section`` is the operator-facing group the rows are exported under (the checkboxes on
    the backup screen: memories, skills, settings, preferences). Several entities may share a
    section — a skill and its files both land in ``skills``.

    ``natural_key`` is what makes a record *the same record* on another host, so a merge-import
    can recognize it (`BACKUP-2`). Name the columns as they are declared, encrypted ones
    included: the importer compares **decrypted** values, since the same content re-sealed on
    another host is a different ciphertext. An empty key means the primary key is the only
    identity, so only a re-import of the very same rows dedupes.

    ``order`` sorts entities within an import, lowest first, so a parent lands before rows that
    reference it (a skill before its files).
    """

    section: str
    natural_key: tuple[str, ...] = ()
    order: int = 0
