"""Export and merge-import of the operator's data (`BACKUP-1`, `BACKUP-2`).

**Export** gathers every table that marked itself for backup (``services/backup/manifest``),
opens each sealed column with the login vault, and re-seals the whole payload under a
separate operator-supplied backup secret (``services/backup/envelope``). The result is one
file with no plainly-readable user data that opens on any other host with nothing but that
secret — the login password is neither needed there nor sufficient here.

**Import** merges. Every incoming record is stamped with the importing operator's
``owner_id`` (the ownership seam) and then tested twice before it is written: once against
the primary key, once against the surface's own natural key — a skill's name, a memory's
text, a preference's key — which is the same identity each surface already enforces in its
own unique constraints. Anything that matches is skipped, so importing the same file twice
changes nothing the second time, and importing a file from another host merges rather than
duplicates.

Ids are **preserved** across an import rather than reissued, which is what keeps a skill's
files attached to their skill and a role's fallback chain pointing at its endpoints. Where
that is not enough is a *skipped parent*: a skill the target already has under its own id
is not written, so every row still carrying the file's id for it would land pointing at a
row this host does not have — silently, since these references carry no foreign key. So the
merge threads an id remap through: a parent skipped in favour of a local twin records
file-id → local-id, and the rows that reference it are re-pointed before they are written.

Note what is deliberately *not* here: the secrets manager (``models/secret``) carries no
backup marker. Its whole point is a second lock; folding its entries into a file protected by
one operator-supplied secret would quietly undo that.
"""

from __future__ import annotations

import asyncio
import base64
import gzip
import io
import json
import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Column, Engine, func
from sqlalchemy.types import TypeDecorator
from sqlmodel import Session, SQLModel, select

from core.db import in_session
from core.exceptions import OdysseusError
from core.vault import Vault
from services.settings_store import SettingsStore

from .envelope import open_envelope, seal
from .manifest import BackupEntity, discover_entities, sections

logger = logging.getLogger(__name__)

# Where the last export's manifest is remembered, so the screen can say what the operator
# last took and when. A preference, not backup machinery — it rides the ordinary settings
# store rather than earning a table.
LAST_EXPORT_KEY = "backup.last_export"


class BackupPayloadError(OdysseusError):
    """The file decrypted, but its contents are not a payload this build understands."""


@dataclass(frozen=True)
class BackupManifestItem:
    """One exported group and how much of it there was."""

    name: str
    count: int


@dataclass(frozen=True)
class BackupManifest:
    created_at: datetime
    items: tuple[BackupManifestItem, ...]

    def to_json(self) -> str:
        return json.dumps(
            {
                "created_at": self.created_at.isoformat(),
                "items": [{"name": i.name, "count": i.count} for i in self.items],
            }
        )

    @classmethod
    def from_json(cls, raw: str) -> BackupManifest | None:
        try:
            data = json.loads(raw)
            return cls(
                created_at=datetime.fromisoformat(data["created_at"]),
                items=tuple(
                    BackupManifestItem(name=i["name"], count=int(i["count"])) for i in data["items"]
                ),
            )
        except ValueError, KeyError, TypeError:
            # A corrupted marker is a missing marker, never a failed page load.
            logger.warning("backup: stored last-export manifest is unreadable")
            return None


@dataclass
class BackupImportReport:
    """What a merge actually did, per group: written, and skipped as already present."""

    imported: dict[str, int] = field(default_factory=dict)
    skipped: dict[str, int] = field(default_factory=dict)
    # Groups in the file this build has no table for — a backup from a newer version.
    unknown: tuple[str, ...] = ()

    def record(self, section: str, *, imported: int, skipped: int) -> None:
        """Fold one entity's outcome into its section's running totals — several entities
        can share a section (a skill and its files), so they accumulate."""
        self.imported[section] = self.imported.get(section, 0) + imported
        self.skipped[section] = self.skipped.get(section, 0) + skipped


def _column_kind(column: Column) -> str:
    """How a column's value is carried in JSON. Driven by the declared type, not by
    sniffing values, so the encoding is exact both ways (a JSON column holding a string
    that *looks* like a timestamp is never re-parsed as one).

    A decorated type is read through to what it decorates: SQLModel declares every
    ``datetime`` field as its own ``UTCDateTime`` decorator over ``DateTime``, and a
    decorator reports no ``python_type`` of its own."""
    column_type = column.type
    if isinstance(column_type, TypeDecorator):
        column_type = column_type.impl_instance
    try:
        python_type = column_type.python_type
    except NotImplementedError:  # JSON and other structural columns
        return "json"
    if python_type is bytes:
        return "bytes"
    if python_type is datetime:
        return "datetime"
    return "plain"


class BackupService:
    def __init__(self, engine: Engine, vault: Vault, settings: SettingsStore) -> None:
        self._engine = engine
        self._vault = vault
        self._settings = settings

    # --- what's in the box ----------------------------------------------------------

    def sections(self) -> tuple[str, ...]:
        """The groups an export can actually produce, discovered from the models."""
        return sections()

    async def counts(self, owner_id: str) -> tuple[BackupManifestItem, ...]:
        """How many records each group would export right now.

        Counted in SQL. This is a readout for a settings screen; loading (and decrypting)
        every row the operator owns just to call ``len`` on it made looking at the backup
        page as expensive as taking a backup."""
        totals: dict[str, int] = {}
        for entity in discover_entities():
            section = entity.spec.section
            totals[section] = totals.get(section, 0) + await self._count(entity, owner_id)
        return tuple(BackupManifestItem(name, count) for name, count in totals.items())

    async def last_manifest(self, owner_id: str) -> BackupManifest | None:
        """The manifest of the last export, or None if the operator has never taken one.
        Absent, never fabricated — the screen shows its own empty state."""
        raw = await self._settings.get(owner_id, LAST_EXPORT_KEY)
        return BackupManifest.from_json(raw) if raw else None

    # --- export (`BACKUP-1`) --------------------------------------------------------

    async def export(
        self, owner_id: str, secret: str, *, include: Sequence[str] | None = None
    ) -> tuple[dict[str, Any], BackupManifest]:
        """Build the encrypted envelope plus the manifest describing what went into it.

        ``include`` names the groups to take (None ⇒ everything). A named group with no
        marked entity behind it contributes nothing and is reported as empty rather than
        invented.
        """
        if not secret:
            raise ValueError("a backup secret is required")
        wanted = None if include is None else set(include)
        totals: dict[str, int] = {name: 0 for name in (wanted or set())}
        created_at = datetime.now(UTC)
        raw = await self._compressed_payload(owner_id, created_at, wanted, totals)
        # Argon2id is deliberately expensive; keep it off the event loop.
        envelope = await asyncio.to_thread(seal, secret, raw, created_at=created_at)

        manifest = BackupManifest(
            created_at=created_at,
            items=tuple(BackupManifestItem(name, count) for name, count in sorted(totals.items())),
        )
        await self._settings.set(owner_id, LAST_EXPORT_KEY, manifest.to_json())
        return envelope, manifest

    # --- import (`BACKUP-2`) --------------------------------------------------------

    async def import_backup(
        self,
        owner_id: str,
        secret: str,
        envelope: Mapping[str, Any],
        *,
        include: Sequence[str] | None = None,
    ) -> BackupImportReport:
        """Merge a backup file into this host's data. Idempotent: the same file imported
        twice writes nothing the second time."""
        raw = await asyncio.to_thread(open_envelope, secret, envelope)
        try:
            payload = json.loads(gzip.decompress(raw))
            file_sections = payload["sections"]
            if not isinstance(file_sections, dict):
                raise TypeError
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise BackupPayloadError("the backup's contents could not be read") from exc

        wanted = None if include is None else set(include)
        report = BackupImportReport()
        known: set[str] = set()
        # file-id → local-id, for every row a merge skipped in favour of one this host
        # already had under a different id. Accumulated across the whole import, not per
        # entity: the rows that need it (a skill's files, a role's fallback chain) are
        # merged in a *later* pass than the parent whose id was replaced.
        remap: dict[str, str] = {}

        # Walk the *entities* in import order, not the file's key order, so a parent
        # (a skill, an endpoint) is always written — or recorded in `remap` — before the
        # rows that point at it.
        for entity in discover_entities():
            section = entity.spec.section
            if wanted is not None and section not in wanted:
                continue
            known.add(entity.name)
            rows = (file_sections.get(section) or {}).get(entity.name)
            if not rows:
                report.record(section, imported=0, skipped=0)
                continue
            imported, skipped = await self._merge(entity, owner_id, rows, remap)
            report.record(section, imported=imported, skipped=skipped)

        in_file = {
            name for group in file_sections.values() if isinstance(group, dict) for name in group
        }
        report.unknown = tuple(sorted(in_file - known))
        return report

    # --- internals ------------------------------------------------------------------

    async def _compressed_payload(
        self,
        owner_id: str,
        created_at: datetime,
        wanted: set[str] | None,
        totals: dict[str, int],
    ) -> bytes:
        """The export document, gzipped, written one entity at a time — and ``totals``
        filled in as it goes.

        Built as a stream rather than as a dict handed to ``json.dumps``. Accumulating the
        whole payload first meant the peak of an export held four copies of the operator's
        data at once: the decoded rows, the assembled JSON string, its UTF-8 bytes, and the
        compressed result. Here the only things alive together are one entity's rows and
        the compressed buffer, so the peak tracks the largest table rather than the
        database. ``discover_entities`` is already ordered by section, so each section's
        object can be opened and closed in one pass without regrouping.
        """
        buffer = io.BytesIO()
        # mtime=0: the document stamps its own `created_at`, and a second, different
        # timestamp in the gzip header is one nothing reads and everything must ignore.
        with gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0) as gz:

            def write(text: str) -> None:
                gz.write(text.encode())

            write('{"created_at": ' + json.dumps(created_at.isoformat()) + ', "sections": {')
            open_section: str | None = None
            first_entity = True
            for entity in discover_entities():
                section = entity.spec.section
                if wanted is not None and section not in wanted:
                    continue
                if section != open_section:
                    write("}, " if open_section is not None else "")
                    write(json.dumps(section) + ": {")
                    open_section = section
                    first_entity = True
                rows = await self._rows(entity, owner_id)
                write(("" if first_entity else ", ") + json.dumps(entity.name) + ": [")
                first_entity = False
                for index, row in enumerate(rows):
                    write((", " if index else "") + json.dumps(self._encode(entity, row)))
                write("]")
                totals[section] = totals.get(section, 0) + len(rows)
            write("}}}" if open_section is not None else "}}")
        return buffer.getvalue()

    async def _count(self, entity: BackupEntity, owner_id: str) -> int:
        model = entity.model

        def work(session: Session) -> int:
            return session.exec(
                select(func.count()).select_from(model).where(model.owner_id == owner_id)
            ).one()

        return await in_session(self._engine, work)

    async def _rows(self, entity: BackupEntity, owner_id: str) -> list[SQLModel]:
        model = entity.model

        def work(session: Session) -> list[SQLModel]:
            return list(session.exec(select(model).where(model.owner_id == owner_id)).all())

        return await in_session(self._engine, work)

    async def _merge(
        self,
        entity: BackupEntity,
        owner_id: str,
        rows: Iterable[Mapping[str, Any]],
        remap: dict[str, str],
    ) -> tuple[int, int]:
        """Merge one entity's rows, reading and extending the import-wide id ``remap``."""
        existing = await self._rows(entity, owner_id)
        seen_ids = {row.id for row in existing}  # type: ignore[attr-defined]
        # Natural key → the id that record already carries *here*. A dict, not a set,
        # because a skipped row has to hand its local id to whatever pointed at the
        # file's id — knowing only "a duplicate exists" is what orphans the children.
        local_by_key: dict[tuple[Any, ...], str] = {}
        for row in existing:
            row_key = self._natural_key(entity, self._plain(entity, row))
            if row_key is not None:
                local_by_key[row_key] = row.id  # type: ignore[attr-defined]

        fresh: list[SQLModel] = []
        skipped = 0
        for incoming in rows:
            values = dict(incoming)
            # The ownership seam: an imported record belongs to whoever imported it.
            values["owner_id"] = owner_id
            # Re-point parent references *before* the identity tests, not after: a child
            # whose parent moved is still the same child, and its natural key may be built
            # from that very reference (SkillFile keys on ("skill_id", "relpath")), so the
            # duplicate check has to run against the local ids or it compares the wrong
            # tuple and re-inserts a file the target already has.
            self._reparent(entity, values, remap)
            key = self._natural_key(entity, values)
            file_id = values.get("id")
            if file_id in seen_ids:
                skipped += 1  # the very same record — references already resolve
                continue
            local_id = None if key is None else local_by_key.get(key)
            if local_id is not None:
                # Present here under a different id: note the substitution so later rows
                # pointing at the file's id are re-pointed rather than left dangling.
                if isinstance(file_id, str) and local_id != file_id:
                    remap[file_id] = local_id
                skipped += 1
                continue
            try:
                instance = self._decode(entity, values)
            except KeyError, ValueError, TypeError:
                logger.warning("backup: dropped an unreadable %s row", entity.name)
                skipped += 1
                continue
            fresh.append(instance)
            seen_ids.add(file_id)
            if key is not None and isinstance(file_id, str):
                # Written under its own id, so it is its own local twin — a second row in
                # the same file with this key now remaps onto it.
                local_by_key[key] = file_id

        if fresh:

            def work(session: Session) -> None:
                for instance in fresh:
                    session.add(instance)
                session.flush()

            await in_session(self._engine, work)
        return len(fresh), skipped

    @staticmethod
    def _reparent(entity: BackupEntity, values: dict[str, Any], remap: Mapping[str, str]) -> None:
        """Rewrite a row's references to ids this host actually uses. In place, no-op when
        nothing has been remapped (the common case — ids are preserved).

        Substitution is by *value*, not against a declared list of foreign-key columns.
        An id is an opaque uuid4 hex, so a column holding one that a parent gave up is a
        reference to that parent whatever the column is named — which covers both shapes
        the schema has today, a scalar (``SkillFile.skill_id``) and a JSON list
        (``ModelRole.endpoint_ids``), and any future one without a new marker to declare
        and keep in sync. The primary key is excluded: a row's own identity is never a
        reference to another row.

        One column deep, deliberately: a reference buried inside a dict-valued JSON column
        or spliced into a longer string is not rewritten. No backed-up entity holds one
        today (``SearchProvider.params`` is provider settings, not ids), and guessing at
        arbitrary nesting would risk mangling opaque operator data to fix nothing."""
        if not remap:
            return
        for column in entity.model.__table__.columns:  # type: ignore[attr-defined]
            if column.primary_key:
                continue
            value = values.get(column.name)
            if isinstance(value, str) and value in remap:
                values[column.name] = remap[value]
            elif isinstance(value, list):
                values[column.name] = [
                    remap.get(item, item) if isinstance(item, str) else item for item in value
                ]

    def _natural_key(
        self, entity: BackupEntity, values: Mapping[str, Any]
    ) -> tuple[Any, ...] | None:
        """The identity two hosts would agree on, read off already-plaintext values. None
        when the entity declared no natural key — then only the primary key dedupes."""
        columns = entity.spec.natural_key
        if not columns:
            return None
        return tuple(values.get(column) for column in columns)

    def _plain(self, entity: BackupEntity, row: SQLModel) -> dict[str, Any]:
        """A DB row's values with its sealed columns opened — the shape an incoming row is
        already in, so the two can be compared without either side guessing."""
        values: dict[str, Any] = {}
        for column in entity.model.__table__.columns:  # type: ignore[attr-defined]
            value = getattr(row, column.name)
            if column.name.endswith("_enc") and value is not None:
                value = self._unseal(value)
            values[column.name] = value
        return values

    def _encode(self, entity: BackupEntity, row: SQLModel) -> dict[str, Any]:
        """One row, JSON-ready: the opened values from :meth:`_plain` (the envelope re-seals
        the whole payload under the backup secret) with bytes/timestamps made portable."""
        plain = self._plain(entity, row)
        return {
            column.name: _to_json(plain[column.name], _column_kind(column))
            for column in entity.model.__table__.columns  # type: ignore[attr-defined]
        }

    def _decode(self, entity: BackupEntity, values: Mapping[str, Any]) -> SQLModel:
        """The reverse: JSON back to column values, re-sealed under *this* host's DEK."""
        built: dict[str, Any] = {}
        for column in entity.model.__table__.columns:  # type: ignore[attr-defined]
            if column.name not in values:
                continue
            value = _from_json(values[column.name], _column_kind(column))
            if column.name.endswith("_enc") and value is not None:
                value = self._seal(value)
            built[column.name] = value
        return entity.model(**built)

    def _unseal(self, value: str | bytes) -> str | bytes:
        return (
            self._vault.decrypt_bytes(value)
            if isinstance(value, bytes)
            else self._vault.decrypt_str(value)
        )

    def _seal(self, value: str | bytes) -> str | bytes:
        return (
            self._vault.encrypt_bytes(value)
            if isinstance(value, bytes)
            else self._vault.encrypt_str(value)
        )


def _to_json(value: Any, kind: str) -> Any:
    if value is None:
        return None
    if kind == "bytes":
        return base64.b64encode(value).decode()
    if kind == "datetime":
        return value.isoformat()
    return value


def _from_json(value: Any, kind: str) -> Any:
    if value is None:
        return None
    if kind == "bytes":
        return base64.b64decode(value)
    if kind == "datetime":
        return datetime.fromisoformat(value)
    return value
