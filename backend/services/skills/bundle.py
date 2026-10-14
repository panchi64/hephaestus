"""The Agent Skills bundle format — parse, validate, render.

A skill is not a schema we invented; it is the **Agent Skills open standard** artifact
(agentskills.io — the same format Claude Code, claude.ai uploads, and the Skills API read):
a directory whose entrypoint is a ``SKILL.md`` carrying YAML frontmatter, alongside whatever
supporting files it needs (``scripts/``, ``references/``, ``assets/``…). Treating that
artifact as the data model is what makes a skill authored here run unchanged in Claude Code,
and a skill downloaded from anywhere run unchanged here.

This module is the whole compliance layer and is deliberately **pure** — no database, no
vault, no disk. It turns bytes into a :class:`ParsedSkill` (or refuses, with the offending
field named) and turns a :class:`ParsedSkill` back into bytes. ``SkillStore`` owns
persistence; the routes own transport; neither re-implements a rule from here.

Two properties worth stating plainly, because they are contracts the tests pin:

* **Round-trip fidelity is semantic, not byte-exact.** The six spec fields round-trip through
  typed attributes and non-spec keys (Claude Code extensions such as ``when_to_use`` or
  ``paths``) survive verbatim in ``extras`` with their original order — but YAML comments and
  quoting style are lost, because we re-render rather than splice. Bundled files are always
  byte-identical.
* **Rejections are hard, warnings are soft.** Anything that would make the bundle invalid
  against the standard raises :class:`SkillValidationError` naming the field. Anything merely
  surprising (an unknown key, an empty body) is a warning the operator sees on import and
  keeps.
"""

from __future__ import annotations

import io
import re
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import cast

import yaml
from pydantic import JsonValue

from core.exceptions import SkillValidationError
from core.frontmatter import FrontmatterError, split_frontmatter

# ── The standard's constraints ───────────────────────────────────────────────────────────
SKILL_FILE = "SKILL.md"
#: The six fields portable across Claude Code, claude.ai uploads, and the Skills API. Any
#: other key is a Claude Code extension (or someone else's) — preserved, never interpreted.
SPEC_FIELDS = ("name", "description", "license", "compatibility", "metadata", "allowed-tools")
NAME_MAX_CHARS = 64
NAME_PATTERN = re.compile(r"^[a-z0-9-]+$")
#: Reserved by the standard so a third-party skill can't pass itself off as first-party.
RESERVED_NAME_WORDS = ("anthropic", "claude")
DESCRIPTION_MAX_CHARS = 1024
COMPATIBILITY_MAX_CHARS = 500
#: The Skills API's uncompressed ceiling. Enforced *before* extraction — a zip-bomb guard,
#: not just a size preference.
BUNDLE_MAX_BYTES = 30 * 1024 * 1024
# Only something shaped like a real tag — `<name …>` or `</name>`. A bare `<`…`>` pair
# is ordinary prose ("use when a CSV has <1000 rows and >3 columns"), and rejecting that
# would block a description containing nothing but comparison operators.
_XML_TAG = re.compile(r"</?[A-Za-z][^>]*>")
# Any whitespace run, including newlines. A description is rendered as a single line in
# the agent's per-turn skill catalog, so embedded newlines would let a published skill
# emit free-standing prose into the instruction block — normalized away at the boundary
# rather than rejected, so a bundle using a YAML block scalar still imports.
_WHITESPACE_RUN = re.compile(r"\s+")
# Entries macOS Finder and friends add alongside the real bundle. Dropped before the
# single-top-level-directory check, which they would otherwise fail.
_SIDECAR_PREFIXES = ("__MACOSX/",)
_SIDECAR_NAMES = (".DS_Store", "Thumbs.db")
_SYMLINK_MODE = 0o120000


@dataclass(frozen=True)
class ParsedSkill:
    """A validated ``SKILL.md`` — the six spec fields, the unknown ones, and the body."""

    name: str
    description: str
    body: str
    license: str | None = None
    compatibility: str | None = None
    metadata: dict[str, JsonValue] | None = None
    allowed_tools: list[str] | None = None
    #: Non-spec frontmatter keys, in their original order, preserved for round-tripping.
    extras: dict[str, JsonValue] = field(default_factory=dict[str, JsonValue])


@dataclass(frozen=True)
class ImportedBundle:
    """A zip (or bare ``SKILL.md``) read into memory, with its supporting files."""

    skill: ParsedSkill
    #: ``(relpath, bytes)`` for every file *except* ``SKILL.md``, which lives in ``skill``.
    files: list[tuple[str, bytes]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# ── Field validators (also called by SkillStore, so REST and tools can't bypass them) ─────


def validate_name(name: object) -> str:
    """The bundle's identity: its directory name, its uniqueness key, and the handle the
    model calls it by. Constraints are the standard's, not ours."""
    if not isinstance(name, str) or not name.strip():
        raise SkillValidationError("name", "a skill needs a name")
    value = name.strip()
    if len(value) > NAME_MAX_CHARS:
        raise SkillValidationError("name", f"name is over {NAME_MAX_CHARS} characters")
    if not NAME_PATTERN.match(value):
        raise SkillValidationError(
            "name", "name may use only lowercase letters, numbers, and hyphens"
        )
    for word in RESERVED_NAME_WORDS:
        if word in value:
            raise SkillValidationError("name", f"name may not contain the reserved word {word!r}")
    return value


def validate_description(description: object) -> str:
    """What the skill does and when to use it — the one field the model reads before deciding
    to open the skill, which is why the standard requires it and caps it."""
    if not isinstance(description, str) or not description.strip():
        raise SkillValidationError("description", "a skill needs a description")
    # Collapse every whitespace run to a single space *before* measuring: the description
    # is a one-line entry in the agent's catalog, so a multi-line one is normalized to the
    # single line it will actually be rendered as, rather than smuggling extra lines into
    # the instruction block.
    value = _WHITESPACE_RUN.sub(" ", description).strip()
    if len(value) > DESCRIPTION_MAX_CHARS:
        raise SkillValidationError(
            "description", f"description is over {DESCRIPTION_MAX_CHARS} characters"
        )
    if _XML_TAG.search(value):
        raise SkillValidationError("description", "description may not contain XML tags")
    return value


def normalize_dir_name(value: str) -> str:
    """The standard matches a bundle's directory to its ``name`` case- and
    underscore-insensitively, so ``Financial_Skill/`` is a valid home for ``financial-skill``."""
    return value.strip().lower().replace("_", "-")


def _json_object(mapping: Mapping[str, object]) -> dict[str, JsonValue]:
    return {str(key): _json_safe(item) for key, item in mapping.items()}


def _json_safe(value: object) -> JsonValue:
    """Coerce YAML's richer scalar set down to what JSON can hold.

    ``yaml.safe_load`` decodes an unquoted ``2025-01-01`` into a ``datetime.date``, which
    the store seals with ``json.dumps`` — so a bundle whose ``metadata`` or non-spec keys
    carry a bare date would otherwise fail deep in a write transaction with a raw
    ``TypeError`` rather than anything the operator can act on. Dates become ISO strings
    (stable across a re-export/re-import cycle, since they round-trip quoted) and any other
    exotic scalar becomes its ``str``."""
    if isinstance(value, dict):
        return _json_object(cast("dict[str, object]", value))
    if isinstance(value, list | tuple):
        return [_json_safe(item) for item in cast("Sequence[object]", value)]
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    if isinstance(value, date | datetime):
        return value.isoformat()
    return str(value)


def _normalize_allowed_tools(raw: object, warnings: list[str]) -> list[str] | None:
    """``allowed-tools`` accepts a space- or comma-separated string or a YAML list. Advisory
    here — recorded and displayed, never enforced. The tool policy is the enforcement
    point; this list arrives with the bundle, from wherever it was imported."""
    if raw is None:
        return None
    if isinstance(raw, str):
        parts = [part for part in re.split(r"[,\s]+", raw) if part]
    elif isinstance(raw, list):
        items = cast("list[object]", raw)
        parts = [str(part).strip() for part in items if str(part).strip()]
    else:
        warnings.append("allowed-tools was not a string or list and was dropped")
        return None
    return parts or None


# ── SKILL.md ─────────────────────────────────────────────────────────────────────────────


def parse_skill_md(text: str, *, warnings: list[str] | None = None) -> ParsedSkill:
    """Split a ``SKILL.md`` into validated frontmatter and body.

    Raises :class:`SkillValidationError` when the artifact isn't a valid skill; appends to
    ``warnings`` for anything preserved-but-surprising."""
    notes = warnings if warnings is not None else []
    try:
        loaded, body = split_frontmatter(text)
    except FrontmatterError as exc:
        # Restated as this format's own error, because the caller is importing a skill and
        # the field a failure is attributed to is what the operator is shown.
        raise SkillValidationError("frontmatter", f"SKILL.md {exc}") from None

    name = validate_name(loaded.get("name"))
    description = validate_description(loaded.get("description"))

    license_ = loaded.get("license")
    if license_ is not None and not isinstance(license_, str):
        raise SkillValidationError("license", "license must be a string")

    compatibility = loaded.get("compatibility")
    if compatibility is not None:
        if not isinstance(compatibility, str):
            raise SkillValidationError("compatibility", "compatibility must be a string")
        if len(compatibility) > COMPATIBILITY_MAX_CHARS:
            raise SkillValidationError(
                "compatibility", f"compatibility is over {COMPATIBILITY_MAX_CHARS} characters"
            )

    # The standard drops a non-map `metadata` rather than failing the bundle; match that
    # so a skill that is otherwise fine still imports.
    raw_metadata = loaded.get("metadata")
    metadata: dict[str, JsonValue] | None = None
    if isinstance(raw_metadata, dict):
        metadata = _json_object(cast("dict[str, object]", raw_metadata))
    elif raw_metadata is not None:
        notes.append("metadata was not a mapping and was dropped")

    allowed_tools = _normalize_allowed_tools(loaded.get("allowed-tools"), notes)
    extras = _json_object({key: value for key, value in loaded.items() if key not in SPEC_FIELDS})
    if extras:
        notes.append(
            "kept non-standard frontmatter keys: " + ", ".join(sorted(extras)),
        )
    if not body:
        notes.append("SKILL.md has no instructions below the frontmatter")

    return ParsedSkill(
        name=name,
        description=description,
        body=body,
        license=license_,
        compatibility=compatibility,
        metadata=metadata,
        allowed_tools=allowed_tools,
        extras=extras,
    )


def render_skill_md(skill: ParsedSkill) -> str:
    """Serialize back to the standard: the spec fields in spec order, then the preserved
    non-spec keys in theirs."""
    front: dict[str, JsonValue] = {"name": skill.name, "description": skill.description}
    if skill.license is not None:
        front["license"] = skill.license
    if skill.compatibility is not None:
        front["compatibility"] = skill.compatibility
    if skill.metadata is not None:
        front["metadata"] = skill.metadata
    if skill.allowed_tools:
        front["allowed-tools"] = list[JsonValue](skill.allowed_tools)
    front.update(skill.extras)
    dumped = yaml.safe_dump(front, sort_keys=False, allow_unicode=True, default_flow_style=False)
    return f"---\n{dumped}---\n\n{skill.body}\n"


# ── Bundles ──────────────────────────────────────────────────────────────────────────────


def _is_sidecar(name: str) -> bool:
    """Archiver droppings that aren't part of the bundle. macOS Finder always writes a
    sibling ``__MACOSX/`` tree, which would otherwise read as a second top-level directory
    and make every Finder-compressed skill un-importable."""
    if name.startswith(_SIDECAR_PREFIXES):
        return True
    base = name.rsplit("/", 1)[-1]
    return base in _SIDECAR_NAMES or base.startswith("._")


def _reject_unsafe_entry(info: zipfile.ZipInfo) -> None:
    """A bundle is extracted into an operator's sandbox and re-exported to other tools, so an
    entry that escapes its root or is a symlink is refused on the way *in* — never quietly
    normalized, and never laundered back out through an export."""
    name = info.filename
    if "\\" in name:
        raise SkillValidationError("bundle", f"entry {name!r} uses backslash path separators")
    if name.startswith("/") or (len(name) > 1 and name[1] == ":"):
        raise SkillValidationError("bundle", f"entry {name!r} is an absolute path")
    if any(part == ".." for part in name.split("/")):
        raise SkillValidationError("bundle", f"entry {name!r} escapes the bundle root")
    if (info.external_attr >> 16) & 0o170000 == _SYMLINK_MODE:
        raise SkillValidationError("bundle", f"entry {name!r} is a symlink")


def read_bundle(data: bytes) -> ImportedBundle:
    """Read a skill zip: a single top-level directory holding ``SKILL.md`` plus its files."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise SkillValidationError("bundle", "not a readable zip archive") from None

    with archive:
        entries = [
            info
            for info in archive.infolist()
            if not info.is_dir() and not _is_sidecar(info.filename)
        ]
        if not entries:
            raise SkillValidationError("bundle", "the archive is empty")
        for info in entries:
            _reject_unsafe_entry(info)

        total = sum(info.file_size for info in entries)
        if total > BUNDLE_MAX_BYTES:
            raise SkillValidationError(
                "bundle", f"the bundle is over {BUNDLE_MAX_BYTES // (1024 * 1024)} MB uncompressed"
            )

        names = {info.filename for info in entries}
        # The standard's shape is one top-level directory, but a zip made by selecting the
        # *contents* of a skill folder puts SKILL.md at the archive root. Both are accepted;
        # only the nested form has a directory name to check against `name`.
        if SKILL_FILE in names:
            root = ""
        else:
            roots = {info.filename.split("/")[0] for info in entries}
            if len(roots) != 1:
                raise SkillValidationError(
                    "bundle", "a skill zip must contain exactly one top-level directory"
                )
            root = roots.pop()
            if f"{root}/{SKILL_FILE}" not in names:
                raise SkillValidationError("bundle", f"the bundle has no {root}/{SKILL_FILE}")

        prefix = f"{root}/" if root else ""
        warnings: list[str] = []
        raw = archive.read(f"{prefix}{SKILL_FILE}")
        try:
            skill = parse_skill_md(raw.decode("utf-8"), warnings=warnings)
        except UnicodeDecodeError:
            raise SkillValidationError("bundle", f"{SKILL_FILE} is not valid UTF-8") from None

        if root and normalize_dir_name(root) != skill.name:
            raise SkillValidationError(
                "name",
                f"the bundle directory {root!r} does not match the skill name {skill.name!r}",
            )

        files: list[tuple[str, bytes]] = []
        for info in sorted(entries, key=lambda i: i.filename):
            if info.filename == f"{prefix}{SKILL_FILE}":
                continue
            files.append((info.filename[len(prefix) :], archive.read(info)))

    return ImportedBundle(skill=skill, files=files, warnings=warnings)


def read_import(data: bytes, filename: str) -> ImportedBundle:
    """Import either shape the operator can plausibly have on hand: a packaged bundle, or a
    lone ``SKILL.md`` (a skill with no supporting files is still a skill)."""
    if filename.lower().endswith(".zip"):
        return read_bundle(data)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise SkillValidationError("bundle", "the file is neither a zip nor UTF-8 text") from None
    warnings: list[str] = []
    return ImportedBundle(skill=parse_skill_md(text, warnings=warnings), warnings=warnings)


def write_bundle(skill: ParsedSkill, files: list[tuple[str, bytes]]) -> bytes:
    """Package a skill for anywhere else: one top-level directory named for the skill, with
    ``SKILL.md`` at its root. Entry order and timestamps are fixed so the same skill always
    exports to the same bytes."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        stamp = (1980, 1, 1, 0, 0, 0)
        entries = [(f"{skill.name}/{SKILL_FILE}", render_skill_md(skill).encode("utf-8"))]
        entries += [(f"{skill.name}/{relpath}", blob) for relpath, blob in sorted(files)]
        for arcname, blob in entries:
            info = zipfile.ZipInfo(arcname, date_time=stamp)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, blob)
    return buffer.getvalue()
