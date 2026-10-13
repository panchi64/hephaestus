"""``SkillStore`` — persistence for Agent Skills bundles (`SKILL-1`…`SKILL-3`).

The one implementation of every skill operation. The REST surface (`routes/skills.py`), the
agent's toolset (`tools/skills.py`), and the per-turn catalog injection (`agent/factory.py`)
are all callers; none of them re-implements a rule from here, and none of them can bypass the
format validators in :mod:`services.skills.bundle` — ``create`` and ``update`` re-run them, so
a skill written through the API is exactly as standard-compliant as one imported from a zip.

Two invariants worth naming:

* **Import always lands as a draft.** A bundle is instructions the agent will follow, so
  ``published`` is the trust boundary rather than a display state: nothing the operator hasn't
  read reaches the model's catalog.
* **Sealing happens here, never in the columns.** Description, body, files, and the preserved
  non-standard frontmatter go through the vault on the way in and out; what the DB must filter
  or order stays clear (see ``models/skill.py`` for the per-column reasoning).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Engine, func
from sqlmodel import Session, select

from core.db import in_session
from core.exceptions import NotFoundError, SkillSpanError, SkillValidationError
from core.text import replace_unique
from core.vault import Vault
from models.skill import Skill, SkillFile, SkillSource
from services.revisions import Bundle, RevisionStore, records

from .bundle import (
    BUNDLE_MAX_BYTES,
    NAME_MAX_CHARS,
    SKILL_FILE,
    ImportedBundle,
    ParsedSkill,
    parse_skill_md,
    read_import,
    render_skill_md,
    validate_description,
    validate_name,
    write_bundle,
)


@dataclass(frozen=True)
class SkillFileView:
    """One supporting file, described without its bytes."""

    relpath: str
    sha256: str
    size_bytes: int


@dataclass(frozen=True)
class SkillCatalogEntry:
    """What the model sees at disclosure level 1 — the whole skill in two fields."""

    name: str
    description: str
    pinned: bool = False
    #: ``platform``, ``project`` or ``plugin`` for skills read from those; else the
    #: stored skill's own source.
    source: str = "authored"


@dataclass(frozen=True)
class SkillSummaryView:
    id: str
    name: str
    description: str
    published: bool
    pinned: bool
    source: str
    file_count: int
    size_bytes: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class SkillView:
    id: str
    name: str
    description: str
    body: str
    published: bool
    source: str
    created_at: datetime
    updated_at: datetime
    pinned: bool = False
    license: str | None = None
    compatibility: str | None = None
    metadata: dict[str, Any] | None = None
    allowed_tools: list[str] | None = None
    extras: dict[str, Any] | None = None
    files: tuple[SkillFileView, ...] = ()

    def parsed(self) -> ParsedSkill:
        """The standard-format view of this skill — what export renders and what the sandbox
        stager writes as ``SKILL.md``."""
        return ParsedSkill(
            name=self.name,
            description=self.description,
            body=self.body,
            license=self.license,
            compatibility=self.compatibility,
            metadata=self.metadata,
            allowed_tools=self.allowed_tools,
            extras=dict(self.extras or {}),
        )


class SkillStore:
    revision_kind = "skill"

    def __init__(
        self, engine: Engine, vault: Vault, revisions: RevisionStore | None = None
    ) -> None:
        self._engine = engine
        self._vault = vault
        self.revisions = revisions

    # ── writes ───────────────────────────────────────────────────────────────────────────

    async def create(
        self,
        owner_id: str,
        *,
        name: str,
        description: str,
        body: str = "",
        license: str | None = None,
        compatibility: str | None = None,
        metadata: dict[str, Any] | None = None,
        allowed_tools: list[str] | None = None,
        extras: dict[str, Any] | None = None,
        source: str = SkillSource.AUTHORED,
        published: bool = False,
        files: list[tuple[str, bytes]] | None = None,
    ) -> SkillView:
        """Create a skill. Draft by default — publishing is a separate, deliberate act."""
        clean_name = validate_name(name)
        clean_description = validate_description(description)

        def work(session: Session) -> SkillView:
            if _find_by_name(session, owner_id, clean_name) is not None:
                raise SkillValidationError("name", f"a skill named {clean_name!r} already exists")
            skill = Skill(
                owner_id=owner_id,
                name=clean_name,
                description_enc=self._vault.encrypt_str(clean_description),
                body_enc=self._vault.encrypt_str(body),
                license=license,
                allowed_tools_json=_dump_list(allowed_tools),
                compatibility_enc=self._seal(compatibility),
                metadata_json_enc=self._seal_json(metadata),
                extras_json_enc=self._seal_json(extras),
                published=published,
                source=source,
            )
            session.add(skill)
            session.flush()
            for relpath, blob in files or []:
                session.add(self._file_row(owner_id, skill.id, relpath, blob))
            session.flush()
            return self._view(session, skill)

        return await in_session(self._engine, work)

    @records("edited")
    async def update(
        self,
        owner_id: str,
        skill_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
        body: str | None = None,
        license: str | None = None,
        compatibility: str | None = None,
        metadata: dict[str, Any] | None = None,
        allowed_tools: list[str] | None = None,
        extras: dict[str, Any] | None = None,
    ) -> SkillView:
        """Full-rewrite edit. Every provided field is re-validated against the standard, so
        the API can't produce a skill that wouldn't survive an export."""
        clean_name = validate_name(name) if name is not None else None
        clean_description = validate_description(description) if description is not None else None

        def work(session: Session) -> SkillView:
            skill = _require(session, owner_id, skill_id)
            if clean_name is not None and clean_name != skill.name:
                if _find_by_name(session, owner_id, clean_name) is not None:
                    raise SkillValidationError(
                        "name", f"a skill named {clean_name!r} already exists"
                    )
                skill.name = clean_name
            if clean_description is not None:
                skill.description_enc = self._vault.encrypt_str(clean_description)
            if body is not None:
                # `set_published` refuses to publish a skill with no instructions; without
                # the same check here, an edit could empty a *already*-published skill's
                # body and leave it in the catalog, so the agent would open it and find
                # nothing to follow. The guard belongs at both ends of the boundary.
                if skill.published and not body.strip():
                    raise SkillValidationError(
                        "body",
                        "a published skill needs instructions — unpublish it first to empty it",
                    )
                skill.body_enc = self._vault.encrypt_str(body)
            if license is not None:
                skill.license = license or None
            if compatibility is not None:
                skill.compatibility_enc = self._seal(compatibility or None)
            if metadata is not None:
                skill.metadata_json_enc = self._seal_json(metadata or None)
            if allowed_tools is not None:
                skill.allowed_tools_json = _dump_list(allowed_tools or None)
            if extras is not None:
                skill.extras_json_enc = self._seal_json(extras or None)
            skill.updated_at = datetime.now(UTC)
            session.add(skill)
            session.flush()
            return self._view(session, skill)

        return await in_session(self._engine, work)

    async def replace_document(self, owner_id: str, skill_id: str, text: str) -> SkillView:
        """Rewrite the skill from a whole ``SKILL.md``. A field the new frontmatter drops is
        cleared."""
        parsed = parse_skill_md(text)
        return await self.update(
            owner_id,
            skill_id,
            name=parsed.name,
            description=parsed.description,
            body=parsed.body,
            license=parsed.license or "",
            compatibility=parsed.compatibility or "",
            metadata=parsed.metadata or {},
            allowed_tools=parsed.allowed_tools or [],
            extras=parsed.extras or {},
        )

    @records("edited")
    async def replace_span(
        self, owner_id: str, skill_id: str, old_text: str, new_text: str
    ) -> SkillView:
        """`SKILL-3` — a small, surgical change to the instructions rather than a full
        rewrite. Raises :class:`SkillSpanError` when ``old_text`` doesn't match exactly one
        span, so the caller can ask for a more precise one. The check and the replace share a
        write transaction, so a concurrent edit can't slip between them."""

        def work(session: Session) -> SkillView:
            skill = _require(session, owner_id, skill_id)
            body = self._vault.decrypt_str(skill.body_enc)
            skill.body_enc = self._vault.encrypt_str(
                replace_unique(body, old_text, new_text, error=SkillSpanError)
            )
            skill.updated_at = datetime.now(UTC)
            session.add(skill)
            session.flush()
            return self._view(session, skill)

        return await in_session(self._engine, work)

    @records("published")
    async def set_published(self, owner_id: str, skill_id: str, published: bool) -> SkillView:
        """Publishing is what makes a skill visible to the agent, so it re-checks that the
        bundle is one the model can actually act on."""

        def work(session: Session) -> SkillView:
            skill = _require(session, owner_id, skill_id)
            if published:
                validate_name(skill.name)
                validate_description(self._vault.decrypt_str(skill.description_enc))
                if not self._vault.decrypt_str(skill.body_enc).strip():
                    raise SkillValidationError(
                        "body", "a skill needs instructions before it can be published"
                    )
            skill.published = published
            skill.updated_at = datetime.now(UTC)
            session.add(skill)
            session.flush()
            return self._view(session, skill)

        return await in_session(self._engine, work)

    async def set_pinned(self, owner_id: str, skill_id: str, pinned: bool) -> SkillView:
        def work(session: Session) -> SkillView:
            skill = _require(session, owner_id, skill_id)
            skill.pinned = pinned
            session.add(skill)
            session.flush()
            return self._view(session, skill)

        return await in_session(self._engine, work)

    async def delete(self, owner_id: str, skill_id: str) -> None:
        """Delete the skill and its whole bundle — no soft-archive; a skill is a tool, and an
        unwanted tool is removed rather than shelved."""

        def work(session: Session) -> None:
            skill = _require(session, owner_id, skill_id)
            for row in session.exec(select(SkillFile).where(SkillFile.skill_id == skill_id)):
                session.delete(row)
            session.delete(skill)

        await in_session(self._engine, work)
        if self.revisions is not None:
            await self.revisions.forget(owner_id, self.revision_kind, skill_id)

    @records("edited")
    async def put_file(self, owner_id: str, skill_id: str, relpath: str, blob: bytes) -> SkillView:
        """Add or replace one supporting file in the bundle."""
        clean = _validate_relpath(relpath)

        def work(session: Session) -> SkillView:
            skill = _require(session, owner_id, skill_id)
            existing = session.exec(
                select(SkillFile)
                .where(SkillFile.skill_id == skill_id)
                .where(SkillFile.relpath == clean)
            ).first()
            # The same ceiling import, export, and sandbox staging all assume. Checked on
            # the way in, because a bundle that only breaks it *after* the write is one the
            # agent silently can't stage and the operator can't export or re-import.
            replaced = existing.size_bytes if existing is not None else 0
            projected = _bundle_bytes(session, skill_id) - replaced + len(blob)
            if projected > BUNDLE_MAX_BYTES:
                raise SkillValidationError(
                    "bundle",
                    f"that file would take the bundle over "
                    f"{BUNDLE_MAX_BYTES // (1024 * 1024)} MB",
                )
            if existing is not None:
                session.delete(existing)
                session.flush()
            session.add(self._file_row(owner_id, skill_id, clean, blob))
            skill.updated_at = datetime.now(UTC)
            session.add(skill)
            session.flush()
            return self._view(session, skill)

        return await in_session(self._engine, work)

    @records("edited")
    async def delete_file(self, owner_id: str, skill_id: str, relpath: str) -> SkillView:
        def work(session: Session) -> SkillView:
            skill = _require(session, owner_id, skill_id)
            row = session.exec(
                select(SkillFile)
                .where(SkillFile.skill_id == skill_id)
                .where(SkillFile.relpath == relpath)
            ).first()
            if row is None:
                raise NotFoundError(f"{relpath} is not in this skill's bundle")
            session.delete(row)
            skill.updated_at = datetime.now(UTC)
            session.add(skill)
            session.flush()
            return self._view(session, skill)

        return await in_session(self._engine, work)

    # ── history ──────────────────────────────────────────────────────────────────────────

    async def snapshot(self, owner_id: str, skill_id: str) -> Bundle:
        """The skill as files: its ``SKILL.md`` and its bundle."""
        view = await self.get(owner_id, skill_id)
        files = dict(await self.file_contents(owner_id, skill_id))
        return {SKILL_FILE: render_skill_md(view.parsed()).encode(), **files}

    working_copy = snapshot

    async def restore_snapshot(self, owner_id: str, skill_id: str, bundle: Bundle) -> SkillView:
        document = bundle.get(SKILL_FILE)
        if document is None:
            raise SkillValidationError("bundle", f"that version has no {SKILL_FILE}")
        await self.replace_document(owner_id, skill_id, document.decode())

        def work(session: Session) -> SkillView:
            skill = _require(session, owner_id, skill_id)
            for row in session.exec(select(SkillFile).where(SkillFile.skill_id == skill_id)):
                session.delete(row)
            session.flush()
            for relpath, blob in sorted(bundle.items()):
                if relpath != SKILL_FILE:
                    session.add(self._file_row(owner_id, skill_id, relpath, blob))
            skill.updated_at = datetime.now(UTC)
            session.add(skill)
            session.flush()
            return self._view(session, skill)

        return await in_session(self._engine, work)

    # ── import / export ──────────────────────────────────────────────────────────────────

    async def import_bundle(
        self, owner_id: str, data: bytes, filename: str
    ) -> tuple[SkillView, list[str]]:
        """Import a packaged bundle (or a lone ``SKILL.md``). **Always lands as a draft** —
        the operator reads it before the agent ever sees it. A name already in the library is
        suffixed rather than refused, so importing a second version of a skill never fails
        halfway through."""
        imported: ImportedBundle = read_import(data, filename)
        parsed = imported.skill
        warnings = list(imported.warnings)

        def work(session: Session) -> SkillView:
            name = _unique_name(session, owner_id, parsed.name)
            if name != parsed.name:
                warnings.append(
                    f"a skill named {parsed.name!r} already existed — imported as {name!r}"
                )
            skill = Skill(
                owner_id=owner_id,
                name=name,
                description_enc=self._vault.encrypt_str(parsed.description),
                body_enc=self._vault.encrypt_str(parsed.body),
                license=parsed.license,
                allowed_tools_json=_dump_list(parsed.allowed_tools),
                compatibility_enc=self._seal(parsed.compatibility),
                metadata_json_enc=self._seal_json(parsed.metadata),
                extras_json_enc=self._seal_json(parsed.extras or None),
                published=False,
                source=SkillSource.IMPORTED,
            )
            session.add(skill)
            session.flush()
            for relpath, blob in imported.files:
                session.add(self._file_row(owner_id, skill.id, _validate_relpath(relpath), blob))
            session.flush()
            return self._view(session, skill)

        view = await in_session(self._engine, work)
        if view.allowed_tools:
            warnings.append(
                "this skill declares allowed-tools; it is recorded and shown, but not enforced"
            )
        return view, warnings

    async def export_bundle(self, owner_id: str, skill_id: str) -> tuple[str, bytes]:
        """Package the skill for anywhere else. Returns ``(filename, zip_bytes)``."""
        view = await self.get(owner_id, skill_id)
        files = await self.file_contents(owner_id, skill_id)
        return f"{view.name}.zip", write_bundle(view.parsed(), files)

    async def file_contents(self, owner_id: str, skill_id: str) -> list[tuple[str, bytes]]:
        """Every supporting file's decrypted bytes, ordered by path — what export zips and
        what the sandbox stager writes."""

        def work(session: Session) -> list[tuple[str, bytes]]:
            _require(session, owner_id, skill_id)
            rows = session.exec(
                select(SkillFile)
                .where(SkillFile.skill_id == skill_id)
                .order_by(SkillFile.relpath)
            ).all()
            return [(row.relpath, self._vault.decrypt_bytes(row.blob_enc)) for row in rows]

        return await in_session(self._engine, work)

    async def file_content(self, owner_id: str, skill_id: str, relpath: str) -> bytes:
        def work(session: Session) -> bytes:
            _require(session, owner_id, skill_id)
            row = session.exec(
                select(SkillFile)
                .where(SkillFile.skill_id == skill_id)
                .where(SkillFile.relpath == relpath)
            ).first()
            if row is None:
                raise NotFoundError(f"{relpath} is not in this skill's bundle")
            return self._vault.decrypt_bytes(row.blob_enc)

        return await in_session(self._engine, work)

    # ── reads ────────────────────────────────────────────────────────────────────────────

    async def get(self, owner_id: str, skill_id: str) -> SkillView:
        def work(session: Session) -> SkillView:
            return self._view(session, _require(session, owner_id, skill_id))

        return await in_session(self._engine, work)

    async def get_by_name(
        self, owner_id: str, name: str, *, published_only: bool = False
    ) -> SkillView:
        """Resolve by the handle the model uses. ``published_only`` makes a draft indistinguishable
        from a skill that doesn't exist — the agent is never told about work in progress."""

        def work(session: Session) -> SkillView:
            skill = _find_by_name(session, owner_id, name.strip().lower())
            if skill is None or (published_only and not skill.published):
                raise NotFoundError(f"no published skill named {name!r}")
            return self._view(session, skill)

        return await in_session(self._engine, work)

    async def list_skills(
        self, owner_id: str, *, published_only: bool = False
    ) -> list[SkillSummaryView]:
        def work(session: Session) -> list[SkillSummaryView]:
            statement = select(Skill).where(Skill.owner_id == owner_id)
            if published_only:
                statement = statement.where(Skill.published)
            skills = session.exec(statement.order_by(Skill.updated_at.desc())).all()
            stats = _file_stats(session, owner_id)
            return [
                SkillSummaryView(
                    id=skill.id,
                    name=skill.name,
                    description=self._vault.decrypt_str(skill.description_enc),
                    published=skill.published,
                    pinned=skill.pinned,
                    source=skill.source,
                    file_count=stats.get(skill.id, (0, 0))[0],
                    size_bytes=stats.get(skill.id, (0, 0))[1],
                    created_at=skill.created_at,
                    updated_at=skill.updated_at,
                )
                for skill in skills
            ]

        return await in_session(self._engine, work)

    async def catalog(self, owner_id: str) -> list[SkillCatalogEntry]:
        """Disclosure level 1 (`SKILL-2`): every **published** skill as name + description,
        newest first so the most recently touched survive the prompt budget."""

        def work(session: Session) -> list[SkillCatalogEntry]:
            skills = session.exec(
                select(Skill)
                .where(Skill.owner_id == owner_id)
                .where(Skill.published)
                .order_by(Skill.updated_at.desc())
            ).all()
            return [
                SkillCatalogEntry(
                    name=skill.name,
                    description=self._vault.decrypt_str(skill.description_enc),
                    pinned=skill.pinned,
                    source=skill.source,
                )
                for skill in skills
            ]

        return await in_session(self._engine, work)

    async def count(self, owner_id: str, *, published_only: bool = False) -> int:
        def work(session: Session) -> int:
            statement = select(func.count()).select_from(Skill).where(Skill.owner_id == owner_id)
            if published_only:
                statement = statement.where(Skill.published)
            return int(session.exec(statement).one())

        return await in_session(self._engine, work)

    # ── internals ────────────────────────────────────────────────────────────────────────

    def _file_row(self, owner_id: str, skill_id: str, relpath: str, blob: bytes) -> SkillFile:
        return SkillFile(
            owner_id=owner_id,
            skill_id=skill_id,
            relpath=relpath,
            sha256=hashlib.sha256(blob).hexdigest(),
            size_bytes=len(blob),
            blob_enc=self._vault.encrypt_bytes(blob),
        )

    def _view(self, session: Session, skill: Skill) -> SkillView:
        rows = session.exec(
            select(SkillFile).where(SkillFile.skill_id == skill.id).order_by(SkillFile.relpath)
        ).all()
        return SkillView(
            id=skill.id,
            name=skill.name,
            description=self._vault.decrypt_str(skill.description_enc),
            body=self._vault.decrypt_str(skill.body_enc),
            published=skill.published,
            source=skill.source,
            created_at=skill.created_at,
            updated_at=skill.updated_at,
            pinned=skill.pinned,
            license=skill.license,
            compatibility=self._open(skill.compatibility_enc),
            metadata=self._open_json(skill.metadata_json_enc),
            allowed_tools=_load_list(skill.allowed_tools_json),
            extras=self._open_json(skill.extras_json_enc),
            files=tuple(
                SkillFileView(relpath=row.relpath, sha256=row.sha256, size_bytes=row.size_bytes)
                for row in rows
            ),
        )

    def _seal(self, value: str | None) -> str | None:
        return None if value is None else self._vault.encrypt_str(value)

    def _open(self, enc: str | None) -> str | None:
        return None if enc is None else self._vault.decrypt_str(enc)

    def _seal_json(self, value: Any | None) -> str | None:
        return None if value is None else self._vault.encrypt_str(json.dumps(value))

    def _open_json(self, enc: str | None) -> Any | None:
        return None if enc is None else json.loads(self._vault.decrypt_str(enc))


# ── module helpers ───────────────────────────────────────────────────────────────────────


def _require(session: Session, owner_id: str, skill_id: str) -> Skill:
    skill = session.get(Skill, skill_id)
    if skill is None or skill.owner_id != owner_id:
        raise NotFoundError("skill not found")
    return skill


def _find_by_name(session: Session, owner_id: str, name: str) -> Skill | None:
    return session.exec(
        select(Skill).where(Skill.owner_id == owner_id).where(Skill.name == name)
    ).first()


def _unique_name(session: Session, owner_id: str, name: str) -> str:
    """``name``, or the first free ``name-2``, ``name-3``… — the same collision walk
    ``attachments_provision`` uses when staging a file whose path is taken."""
    if _find_by_name(session, owner_id, name) is None:
        return name
    for suffix in range(2, 1000):
        tail = f"-{suffix}"
        candidate = f"{name[: NAME_MAX_CHARS - len(tail)]}{tail}"
        if _find_by_name(session, owner_id, candidate) is None:
            return candidate
    raise SkillValidationError("name", f"too many skills named like {name!r}")


def _bundle_bytes(session: Session, skill_id: str) -> int:
    """Current uncompressed size of one skill's supporting files."""
    total = session.exec(
        select(func.coalesce(func.sum(SkillFile.size_bytes), 0)).where(
            SkillFile.skill_id == skill_id
        )
    ).one()
    return int(total)


def _file_stats(session: Session, owner_id: str) -> dict[str, tuple[int, int]]:
    """``skill_id -> (file_count, total_bytes)`` in one grouped query, so listing the library
    never decrypts a blob just to size it."""
    rows = session.exec(
        select(SkillFile.skill_id, func.count(), func.coalesce(func.sum(SkillFile.size_bytes), 0))
        .where(SkillFile.owner_id == owner_id)
        .group_by(SkillFile.skill_id)
    ).all()
    return {skill_id: (int(count), int(total)) for skill_id, count, total in rows}


def _validate_relpath(relpath: str) -> str:
    """A bundle path is written into the sandbox and re-exported to other tools, so it is
    checked here too — not only at the zip boundary — because tools and REST can add files
    that never went through :func:`read_bundle`."""
    clean = relpath.strip()
    # Strip a leading "./" as a *prefix* — never with lstrip(), which takes a character set
    # and would quietly turn "../escape.sh" into a valid-looking "escape.sh".
    while clean.startswith("./"):
        clean = clean[2:]
    if not clean:
        raise SkillValidationError("relpath", "a bundle file needs a path")
    if clean.upper() == "SKILL.MD":
        raise SkillValidationError(
            "relpath", "SKILL.md is the skill's own body, not a bundle file"
        )
    if "\\" in clean or clean.startswith("/") or any(part == ".." for part in clean.split("/")):
        raise SkillValidationError("relpath", f"{relpath!r} escapes the bundle root")
    return clean


def _dump_list(value: list[str] | None) -> str | None:
    return None if not value else json.dumps(value)


def _load_list(raw: str | None) -> list[str] | None:
    return None if not raw else list(json.loads(raw))
