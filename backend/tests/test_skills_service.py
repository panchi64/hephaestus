"""The skills capability: CRUD, publishing as the trust boundary, import/export, sealing."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from pydantic import TypeAdapter
from sqlmodel import Session, select

from core.db import init_db, make_engine
from core.exceptions import NotFoundError, SkillSpanError, SkillValidationError
from core.vault import Vault
from models.skill import Skill, SkillFile, SkillSource
from services.skills import SkillStore, write_bundle
from services.skills.bundle import ParsedSkill

from .test_skills_bundle import SKILL_MD, zip_entries

OWNER = "operator"


async def _store() -> SkillStore:
    """A SkillStore over a throwaway in-memory DB and a temp-dir vault."""
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    vault = Vault(Path(tempfile.mkdtemp()) / "keyfile.json")
    await vault.setup("pw")
    return SkillStore(engine, vault)


def _bundle_zip() -> bytes:
    return zip_entries(
        {
            "pdf-processing/SKILL.md": SKILL_MD.encode(),
            "pdf-processing/scripts/fill.py": b"print('filling')\n",
        }
    )


async def _seeded(
    store: SkillStore,
    *,
    name: str = "release-notes",
    description: str = "Draft release notes from a changelog.",
    body: str = "# Release notes\n\nCollect the merged PRs, group by area.",
):
    return await store.create(OWNER, name=name, description=description, body=body)


# ── SKILL-1: create, view, edit, publish, delete ─────────────────────────────────────────


async def test_create_lands_as_a_draft():
    store = await _store()
    view = await _seeded(store)
    assert view.published is False
    assert view.source == SkillSource.AUTHORED


async def test_create_validates_against_the_standard():
    store = await _store()
    with pytest.raises(SkillValidationError) as caught:
        _ = await _seeded(store, name="Not A Slug")
    assert caught.value.field == "name"


async def test_names_are_unique_per_owner():
    store = await _store()
    _ = await _seeded(store)
    with pytest.raises(SkillValidationError, match="already exists"):
        _ = await _seeded(store)


async def test_update_rewrites_and_revalidates():
    store = await _store()
    view = await _seeded(store)
    updated = await store.update(OWNER, view.id, body="# New body", description="Now different.")
    assert updated.body == "# New body"
    assert updated.description == "Now different."
    with pytest.raises(SkillValidationError):
        _ = await store.update(OWNER, view.id, description="")


async def test_delete_takes_the_bundle_with_it():
    store = await _store()
    view, _ = await store.import_bundle(OWNER, _bundle_zip(), "pdf-processing.zip")
    await store.delete(OWNER, view.id)
    with pytest.raises(NotFoundError):
        _ = await store.get(OWNER, view.id)

    def work(session: Session) -> int:
        return len(session.exec(select(SkillFile).where(SkillFile.skill_id == view.id)).all())

    from core.db import in_session

    assert await in_session(store._engine, work) == 0


async def test_another_owner_cannot_reach_a_skill():
    store = await _store()
    view = await _seeded(store)
    with pytest.raises(NotFoundError):
        _ = await store.get("someone-else", view.id)


# ── SKILL-2: publishing is what the agent can see ────────────────────────────────────────


async def test_catalog_lists_published_skills_only():
    store = await _store()
    draft = await _seeded(store)
    live = await _seeded(store, name="triage-bugs", description="Triage an incoming bug report.")
    _ = await store.set_published(OWNER, live.id, True)

    catalog = await store.catalog(OWNER)
    assert [entry.name for entry in catalog] == ["triage-bugs"]
    assert draft.name not in {entry.name for entry in catalog}


async def test_get_by_name_hides_drafts_when_published_only():
    store = await _store()
    view = await _seeded(store)
    assert (await store.get_by_name(OWNER, "release-notes")).id == view.id
    with pytest.raises(NotFoundError):
        _ = await store.get_by_name(OWNER, "release-notes", published_only=True)


async def test_publishing_refuses_a_skill_with_no_instructions():
    store = await _store()
    view = await _seeded(store, body="")
    with pytest.raises(SkillValidationError) as caught:
        _ = await store.set_published(OWNER, view.id, True)
    assert caught.value.field == "body"


async def test_unpublish_pulls_it_back_out_of_the_catalog():
    store = await _store()
    view = await _seeded(store)
    _ = await store.set_published(OWNER, view.id, True)
    _ = await store.set_published(OWNER, view.id, False)
    assert await store.catalog(OWNER) == []


# ── SKILL-3: surgical edits ──────────────────────────────────────────────────────────────


async def test_replace_span_edits_one_span():
    store = await _store()
    view = await _seeded(store)
    edited = await store.replace_span(OWNER, view.id, "group by area", "group by author")
    assert "group by author" in edited.body
    assert "group by area" not in edited.body


@pytest.mark.parametrize(
    ("body", "old", "expected_occurrences"),
    [("a b c", "zzz", 0), ("dup dup", "dup", 2)],
)
async def test_replace_span_refuses_an_unclear_span(body: str, old: str, expected_occurrences: int):
    store = await _store()
    view = await _seeded(store, body=body)
    with pytest.raises(SkillSpanError) as caught:
        _ = await store.replace_span(OWNER, view.id, old, "x")
    assert caught.value.occurrences == expected_occurrences


# ── Import / export ──────────────────────────────────────────────────────────────────────


async def test_import_lands_as_a_draft_marked_imported():
    store = await _store()
    view, warnings = await store.import_bundle(OWNER, _bundle_zip(), "pdf-processing.zip")
    assert view.published is False
    assert view.source == SkillSource.IMPORTED
    assert view.name == "pdf-processing"
    assert [f.relpath for f in view.files] == ["scripts/fill.py"]
    assert any("allowed-tools" in note for note in warnings)


async def test_import_preserves_non_standard_frontmatter():
    store = await _store()
    view, _ = await store.import_bundle(OWNER, _bundle_zip(), "pdf-processing.zip")
    assert view.extras == {
        "when_to_use": "When the user mentions PDFs, forms, or scanned documents."
    }
    assert view.metadata == {"version": "1.0"}
    assert view.license == "Apache-2.0"
    assert view.allowed_tools == ["Read", "Bash"]


async def test_importing_the_same_skill_twice_suffixes_the_name():
    store = await _store()
    _ = await store.import_bundle(OWNER, _bundle_zip(), "pdf-processing.zip")
    second, warnings = await store.import_bundle(OWNER, _bundle_zip(), "pdf-processing.zip")
    assert second.name == "pdf-processing-2"
    assert any("already existed" in note for note in warnings)


async def test_import_accepts_a_bare_skill_md():
    store = await _store()
    view, _ = await store.import_bundle(OWNER, SKILL_MD.encode(), "SKILL.md")
    assert view.name == "pdf-processing"
    assert view.files == ()


async def test_export_round_trips_back_through_import():
    """The interop contract at the store level: export → re-import is lossless."""
    store = await _store()
    view, _ = await store.import_bundle(OWNER, _bundle_zip(), "pdf-processing.zip")
    filename, data = await store.export_bundle(OWNER, view.id)
    assert filename == "pdf-processing.zip"

    reimported, _ = await store.import_bundle(OWNER, data, filename)
    assert reimported.description == view.description
    assert reimported.body == view.body
    assert reimported.extras == view.extras
    assert reimported.metadata == view.metadata
    assert reimported.allowed_tools == view.allowed_tools
    assert await store.file_contents(OWNER, reimported.id) == await store.file_contents(
        OWNER, view.id
    )


async def test_exported_bytes_match_a_bundle_written_from_the_same_skill():
    store = await _store()
    view, _ = await store.import_bundle(OWNER, _bundle_zip(), "pdf-processing.zip")
    _, data = await store.export_bundle(OWNER, view.id)
    expected = write_bundle(view.parsed(), await store.file_contents(OWNER, view.id))
    assert data == expected


# ── Bundle files ─────────────────────────────────────────────────────────────────────────


async def test_put_file_replaces_by_path():
    store = await _store()
    view = await _seeded(store)
    _ = await store.put_file(OWNER, view.id, "scripts/run.sh", b"echo one")
    updated = await store.put_file(OWNER, view.id, "scripts/run.sh", b"echo two")
    assert len(updated.files) == 1
    assert await store.file_content(OWNER, view.id, "scripts/run.sh") == b"echo two"


@pytest.mark.parametrize("relpath", ["../escape.sh", "/etc/passwd", "SKILL.md", ""])
async def test_put_file_refuses_an_unsafe_or_reserved_path(relpath: str):
    store = await _store()
    view = await _seeded(store)
    with pytest.raises(SkillValidationError):
        _ = await store.put_file(OWNER, view.id, relpath, b"x")


async def test_delete_file_removes_it_from_the_bundle():
    store = await _store()
    view = await _seeded(store)
    _ = await store.put_file(OWNER, view.id, "notes.md", b"x")
    assert (await store.delete_file(OWNER, view.id, "notes.md")).files == ()


async def test_listing_reports_bundle_size_without_decrypting():
    store = await _store()
    view, _ = await store.import_bundle(OWNER, _bundle_zip(), "pdf-processing.zip")
    summary = next(s for s in await store.list_skills(OWNER) if s.id == view.id)
    assert summary.file_count == 1
    assert summary.size_bytes == len(b"print('filling')\n")


# ── At rest ──────────────────────────────────────────────────────────────────────────────


async def test_content_is_sealed_at_rest_and_structure_is_not():
    store = await _store()
    view, _ = await store.import_bundle(OWNER, _bundle_zip(), "pdf-processing.zip")

    def work(session: Session) -> tuple[Skill, SkillFile]:
        skill = session.get(Skill, view.id)
        assert skill is not None
        row = session.exec(select(SkillFile).where(SkillFile.skill_id == view.id)).first()
        assert row is not None
        return skill, row

    from core.db import in_session

    skill, row = await in_session(store._engine, work)
    assert "Fill and extract" not in skill.description_enc
    assert "PDF processing" not in skill.body_enc
    assert b"filling" not in row.blob_enc
    # …while what the DB must filter and stage by stays readable.
    assert skill.name == "pdf-processing"
    assert row.relpath == "scripts/fill.py"


async def test_parsed_view_renders_the_standard_shape():
    store = await _store()
    view, _ = await store.import_bundle(OWNER, _bundle_zip(), "pdf-processing.zip")
    parsed = view.parsed()
    assert isinstance(parsed, ParsedSkill)
    assert parsed.name == "pdf-processing"
    assert (
        TypeAdapter(str)
        .validate_python(parsed.extras["when_to_use"])
        .startswith("When the user mentions")
    )


# ── Regressions ──────────────────────────────────────────────────────────────────────────


async def test_update_refuses_to_empty_a_published_skill():
    """`set_published` guards the publish boundary; an edit must not walk around it."""
    store = await _store()
    view = await _seeded(store)
    _ = await store.set_published(OWNER, view.id, True)
    with pytest.raises(SkillValidationError) as caught:
        _ = await store.update(OWNER, view.id, body="   ")
    assert caught.value.field == "body"
    assert (await store.get(OWNER, view.id)).body.strip() != ""


async def test_a_draft_may_still_be_emptied():
    store = await _store()
    view = await _seeded(store)
    assert (await store.update(OWNER, view.id, body="")).body == ""


async def test_put_file_refuses_to_exceed_the_bundle_ceiling():
    """The 30 MB ceiling import, export, and sandbox staging all assume."""
    from services.skills.bundle import BUNDLE_MAX_BYTES

    store = await _store()
    view = await _seeded(store)
    with pytest.raises(SkillValidationError) as caught:
        _ = await store.put_file(OWNER, view.id, "big.bin", b"\0" * (BUNDLE_MAX_BYTES + 1))
    assert caught.value.field == "bundle"
    assert (await store.get(OWNER, view.id)).files == ()


async def test_replacing_a_file_counts_only_the_difference():
    """A same-path replace frees the old bytes, so it must not be measured as a new file."""
    store = await _store()
    view = await _seeded(store)
    _ = await store.put_file(OWNER, view.id, "a.bin", b"\0" * 1024)
    updated = await store.put_file(OWNER, view.id, "a.bin", b"\0" * 2048)
    assert updated.files[0].size_bytes == 2048


async def test_importing_a_bundle_with_a_yaml_date_succeeds():
    store = await _store()
    text = SKILL_MD.replace('  version: "1.0"', '  version: "1.0"\n  updated: 2025-01-01')
    view, _ = await store.import_bundle(
        OWNER, zip_entries({"pdf-processing/SKILL.md": text.encode()}), "pdf-processing.zip"
    )
    assert view.metadata == {"version": "1.0", "updated": "2025-01-01"}
