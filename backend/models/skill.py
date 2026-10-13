"""Skills schema — a portable Agent Skills bundle, stored decomposed.

A skill is reusable know-how the agent applies to future tasks (`SKILL-1`…`SKILL-3`), and
its on-disk form is the **Agent Skills open standard** artifact: a ``SKILL.md`` with YAML
frontmatter plus whatever supporting files it ships (``scripts/``, ``references/``,
``assets/``). That artifact is the data model — these tables store it, they don't replace it,
which is what lets a skill move between here and any other Agent Skills reader in either
direction, imported and exported unchanged.

**Decomposed, not a sealed zip.** The bundle is split across two tables rather than kept as
one opaque blob because all three requirements need its parts addressable: the per-turn
catalog injection (`SKILL-2`) reads name+description without touching file bytes, the
surgical edit (`SKILL-3`) needs the instruction body as its own column, and staging into the
sandbox writes files straight through with nothing to unzip. An ``Upload`` gets away with a
single ``blob_enc`` because an upload is opaque; a skill is structured.

At-rest posture follows documents and uploads: the content the skill *is* — its
description, its instructions, its files' bytes, and any preserved non-standard frontmatter
— is sealed under the vault. What the database must index or the operator must be able to
filter stays in the clear: ``owner_id``, timestamps, the ``published`` flag, the ``source``,
each file's ``relpath``/``sha256``, and the ``name``. ``name`` in the clear is a deliberate,
slightly weaker posture than the rest: it is simultaneously the uniqueness key, the bundle's
directory name, and the handle the model calls the skill by, so it cannot be ciphertext.
What that leaks is the operator's list of skill names — enough to infer what they work on,
which is the accepted cost of the name being addressable at all.

``SKILL.md`` itself is **not** a ``SkillFile`` row: it is ``body_enc`` plus the frontmatter
columns, rendered on export, so the two can never drift out of agreement.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from sqlmodel import Field, SQLModel, UniqueConstraint

from models._backup import BackupSpec
from models._fields import new_id, utcnow


class SkillSource(StrEnum):
    """Where a skill came from. A plain string column carries it (matching
    ``DocumentVersionOrigin``), with this enum as the in-code vocabulary.

    ``AGENT`` is also the reserved seam for `SKILL-4`: when auto-publishing from the
    verifier's successful recoveries lands, it needs exactly this provenance to decide what
    is eligible."""

    AUTHORED = "authored"
    IMPORTED = "imported"
    AGENT = "agent"


class Skill(SQLModel, table=True):
    __tablename__ = "skills"
    __table_args__ = (UniqueConstraint("owner_id", "name", name="uq_skills_owner_name"),)
    # Exported under "skills" (`BACKUP-1`), before its files. ``name`` is already the
    # library's uniqueness key, so it is the merge key too.
    __backup__ = BackupSpec(section="skills", natural_key=("name",), order=0)

    id: str = Field(default_factory=new_id, primary_key=True)
    owner_id: str = Field(index=True)
    # The standard's slug: lowercase/digits/hyphens, ≤64 chars, and the bundle's directory
    # name on export. Clear because it is the uniqueness key and the model's handle.
    name: str = Field(index=True)
    # AEAD ciphertext of the skill's content: what it's for, and the instructions themselves
    # (the SKILL.md body, below the frontmatter).
    description_enc: str
    body_enc: str
    # Spec frontmatter that is policy rather than prose, kept clear so the library can
    # filter and display it without a decrypt: an SPDX-ish license id, and the advisory
    # allowed-tools list as a JSON array. Advisory only: it is recorded and displayed,
    # never enforced — the tool policy is the enforcement point, not a skill's own
    # frontmatter, which arrives from wherever the bundle was imported from.
    license: str | None = Field(default=None)
    allowed_tools_json: str | None = Field(default=None)
    # Sealed spec frontmatter that may carry operator detail: the compatibility string and
    # the free-form metadata map, each as JSON.
    compatibility_enc: str | None = Field(default=None)
    metadata_json_enc: str | None = Field(default=None)
    # Non-spec frontmatter keys (Claude Code extensions like `when_to_use` or `paths`),
    # sealed as JSON with their original order preserved. Round-tripping these unchanged is
    # what keeps an imported skill exportable back to the tool it came from.
    extras_json_enc: str | None = Field(default=None)
    # The trust boundary, not a display state: the agent's catalog lists published skills
    # only, so an imported bundle is inert until the operator has read it and said yes.
    published: bool = Field(default=False, index=True)
    # Listed by name in every brief, where other skills are found through `platform_search`.
    pinned: bool = Field(default=False)
    # authored | imported | agent — SkillSource is the in-code vocabulary.
    source: str = Field(default=SkillSource.AUTHORED, index=True)
    created_at: datetime = Field(default_factory=utcnow, index=True)
    updated_at: datetime = Field(default_factory=utcnow, index=True)


class SkillFile(SQLModel, table=True):
    """One supporting file in a skill's bundle — a script, a reference doc, an asset."""

    __tablename__ = "skill_files"
    __table_args__ = (UniqueConstraint("skill_id", "relpath", name="uq_skill_files_skill_relpath"),)
    # Rides its skill's section and lands after it. Ids are preserved across an import, so
    # ``skill_id`` still points at the right bundle on the far host.
    __backup__ = BackupSpec(section="skills", natural_key=("skill_id", "relpath"), order=1)

    id: str = Field(default_factory=new_id, primary_key=True)
    owner_id: str = Field(index=True)
    skill_id: str = Field(index=True)
    # The path within the bundle ("scripts/fill.py"). Structural: the library lists a
    # bundle's contents and the sandbox stager builds its target paths from these without
    # decrypting a single blob.
    relpath: str
    # Content digest — the same dedup/identity key uploads use, and what makes re-staging
    # into a warm sandbox a byte-compare instead of a rewrite.
    sha256: str
    size_bytes: int = Field(default=0)
    # AEAD ciphertext of the file's bytes.
    blob_enc: bytes
    created_at: datetime = Field(default_factory=utcnow)
