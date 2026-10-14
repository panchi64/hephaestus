"""`BACKUP-2`: importing merges — no duplicates, every record stamped with the operator."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import Engine
from sqlmodel import Session, select

from core.db import init_db, make_engine
from core.vault import Vault
from models.memory import Memory
from models.registry import ModelRole
from models.skill import Skill, SkillFile
from services.backup import BackupFormatError, BackupSecretError, BackupService
from services.memory import MemoryStore
from services.registry import ModelRegistry
from services.settings_store import SettingsStore
from services.skills import SkillStore

from .test_memory import DegradedEmbedder

OWNER = "operator"
OTHER_OWNER = "someone-else"
BACKUP_SECRET = "correct horse battery staple"


async def _host(tmp_path: Path, name: str) -> tuple[Engine, Vault, BackupService]:
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    vault = Vault(tmp_path / f"{name}-keyfile.json")
    await vault.setup(f"{name}-password")
    return engine, vault, BackupService(engine, vault, SettingsStore(engine))


def _memories(engine: Engine, vault: Vault) -> MemoryStore:
    return MemoryStore(engine, vault, DegradedEmbedder())


def _count(
    engine: Engine, model: type[Memory] | type[Skill] | type[SkillFile], owner_id: str = OWNER
) -> int:
    with Session(engine) as session:
        return len(list(session.exec(select(model).where(model.owner_id == owner_id)).all()))


async def test_reimporting_the_same_file_twice_changes_nothing(tmp_path: Path):
    source_engine, source_vault, source = await _host(tmp_path, "source")
    store = _memories(source_engine, source_vault)
    _ = await store.remember(OWNER, "Deploys go out on Thursdays")
    _ = await store.remember(OWNER, "The wifi password is on the fridge")
    envelope, _ = await source.export(OWNER, BACKUP_SECRET)

    target_engine, _target_vault, target = await _host(tmp_path, "target")
    first = await target.import_backup(OWNER, BACKUP_SECRET, envelope)
    assert first.imported["memories"] == 2
    assert first.skipped["memories"] == 0

    second = await target.import_backup(OWNER, BACKUP_SECRET, envelope)
    assert second.imported["memories"] == 0
    assert second.skipped["memories"] == 2
    assert _count(target_engine, Memory) == 2


async def test_a_record_that_exists_by_natural_key_is_not_duplicated(tmp_path: Path):
    source_engine, source_vault, source = await _host(tmp_path, "source")
    _ = await _memories(source_engine, source_vault).remember(OWNER, "Deploys go out on Thursdays")
    envelope, _ = await source.export(OWNER, BACKUP_SECRET)

    # The target already knows this fact — same content, its own row, its own id.
    target_engine, target_vault, target = await _host(tmp_path, "target")
    _ = await _memories(target_engine, target_vault).remember(OWNER, "Deploys go out on Thursdays")

    report = await target.import_backup(OWNER, BACKUP_SECRET, envelope)
    assert report.imported["memories"] == 0
    assert report.skipped["memories"] == 1
    assert _count(target_engine, Memory) == 1


async def test_imported_records_are_attributed_to_the_importing_operator(tmp_path: Path):
    source_engine, source_vault, source = await _host(tmp_path, "source")
    # Exported by one owner…
    _ = await _memories(source_engine, source_vault).remember(OTHER_OWNER, "A fact")
    envelope, _ = await source.export(OTHER_OWNER, BACKUP_SECRET)

    # …imported by another: the ownership seam is re-stamped, not carried over.
    target_engine, _target_vault, target = await _host(tmp_path, "target")
    report = await target.import_backup(OWNER, BACKUP_SECRET, envelope)

    assert report.imported["memories"] == 1
    assert _count(target_engine, Memory, OWNER) == 1
    assert _count(target_engine, Memory, OTHER_OWNER) == 0


async def test_a_skill_and_its_files_survive_the_round_trip(tmp_path: Path):
    source_engine, source_vault, source = await _host(tmp_path, "source")
    skills = SkillStore(source_engine, source_vault)
    skill = await skills.create(
        OWNER, name="deploy-runbook", description="How we ship", body="1. Push."
    )
    _ = await skills.put_file(OWNER, skill.id, "scripts/ship.sh", b"#!/bin/sh\necho shipping\n")
    envelope, _ = await source.export(OWNER, BACKUP_SECRET, include=["skills"])

    target_engine, target_vault, target = await _host(tmp_path, "target")
    report = await target.import_backup(OWNER, BACKUP_SECRET, envelope)
    assert report.imported["skills"] == 2  # the skill plus its one file

    restored = await SkillStore(target_engine, target_vault).list_skills(OWNER)
    assert [s.name for s in restored] == ["deploy-runbook"]
    # Ids are preserved, so the file arrives still attached to its skill — nothing to remap.
    assert _count(target_engine, SkillFile) == 1
    body = await SkillStore(target_engine, target_vault).file_content(
        OWNER, restored[0].id, "scripts/ship.sh"
    )
    assert body == b"#!/bin/sh\necho shipping\n"

    # And a second import of the same bundle adds nothing.
    again = await target.import_backup(OWNER, BACKUP_SECRET, envelope)
    assert again.imported["skills"] == 0
    assert _count(target_engine, Skill) == 1


async def test_include_limits_what_a_restore_touches(tmp_path: Path):
    source_engine, source_vault, source = await _host(tmp_path, "source")
    _ = await _memories(source_engine, source_vault).remember(OWNER, "A fact")
    _ = await SkillStore(source_engine, source_vault).create(
        OWNER, name="a-skill", description="d", body="b"
    )
    envelope, _ = await source.export(OWNER, BACKUP_SECRET)

    target_engine, _, target = await _host(tmp_path, "target")
    report = await target.import_backup(OWNER, BACKUP_SECRET, envelope, include=["memories"])
    assert report.imported["memories"] == 1
    assert "skills" not in report.imported
    assert _count(target_engine, Skill) == 0


async def test_a_wrong_secret_or_a_foreign_file_is_refused(tmp_path: Path):
    source_engine, source_vault, source = await _host(tmp_path, "source")
    _ = await _memories(source_engine, source_vault).remember(OWNER, "A fact")
    envelope, _ = await source.export(OWNER, BACKUP_SECRET)

    _, _, target = await _host(tmp_path, "target")
    with pytest.raises(BackupSecretError):
        _ = await target.import_backup(OWNER, "wrong-secret", envelope)
    with pytest.raises(BackupFormatError):
        _ = await target.import_backup(OWNER, BACKUP_SECRET, {"format": "something-else"})


# --- regression: a skipped parent must not orphan its children -------------


async def test_files_of_a_skipped_skill_land_on_the_local_skill(tmp_path: Path):
    """The target already has a "triage" skill under its own id, so the incoming skill is
    skipped by natural key — but its files are still imported. Carrying the *file's*
    skill_id they insert pointing at a skill this host has never had: nothing errors
    (skill_id has no foreign key), the rows are just unreachable from the skill they
    belong to, and the operator sees a restored skill with none of its files."""
    source_engine, source_vault, source = await _host(tmp_path, "source")
    source_skills = SkillStore(source_engine, source_vault)
    skill = await source_skills.create(
        OWNER, name="triage", description="How we triage", body="1. Read the page."
    )
    _ = await source_skills.put_file(OWNER, skill.id, "run.sh", b"#!/bin/sh\necho triage\n")
    envelope, _ = await source.export(OWNER, BACKUP_SECRET, include=["skills"])

    target_engine, target_vault, target = await _host(tmp_path, "target")
    target_skills = SkillStore(target_engine, target_vault)
    local = await target_skills.create(
        OWNER, name="triage", description="The target's own", body="1. Ignore it."
    )
    assert local.id != skill.id  # same skill by name, different id — the whole point

    report = await target.import_backup(OWNER, BACKUP_SECRET, envelope)
    assert report.skipped["skills"] == 1  # the skill itself, already present
    assert report.imported["skills"] == 1  # its file, which the target lacked

    with Session(target_engine) as session:
        files = list(session.exec(select(SkillFile)).all())
    assert [f.skill_id for f in files] == [local.id], "the file must hang off the local skill"
    body = await target_skills.file_content(OWNER, local.id, "run.sh")
    assert body == b"#!/bin/sh\necho triage\n"


async def test_a_file_the_local_skill_already_has_is_not_duplicated(tmp_path: Path):
    """The corollary: once the child's reference is rewritten, its natural key
    ("skill_id", "relpath") lines up with the local row's, so the duplicate is recognized
    and skipped. Reparenting *after* the identity test would compare the file's skill_id,
    miss the match, and insert a second copy of a file the skill already has."""
    source_engine, source_vault, source = await _host(tmp_path, "source")
    source_skills = SkillStore(source_engine, source_vault)
    skill = await source_skills.create(OWNER, name="triage", description="d", body="b")
    _ = await source_skills.put_file(OWNER, skill.id, "run.sh", b"from the backup\n")
    envelope, _ = await source.export(OWNER, BACKUP_SECRET, include=["skills"])

    target_engine, target_vault, target = await _host(tmp_path, "target")
    target_skills = SkillStore(target_engine, target_vault)
    local = await target_skills.create(OWNER, name="triage", description="d", body="b")
    _ = await target_skills.put_file(OWNER, local.id, "run.sh", b"already here\n")

    report = await target.import_backup(OWNER, BACKUP_SECRET, envelope)
    assert report.imported["skills"] == 0
    assert _count(target_engine, SkillFile) == 1
    # The local copy wins — a merge never overwrites what this host already decided.
    assert await target_skills.file_content(OWNER, local.id, "run.sh") == b"already here\n"


async def test_a_role_chain_repoints_at_a_skipped_endpoint(tmp_path: Path):
    """Same shape, list-valued: ModelRole.endpoint_ids is a JSON array of endpoint ids.
    The target already has an endpoint named "workstation", so the incoming one is
    skipped — and the role that binds it would restore pointing at an id that resolves to
    nothing, leaving the operator with a role bound to a phantom endpoint."""
    source_engine, source_vault, source = await _host(tmp_path, "source")
    source_registry = ModelRegistry(source_engine, source_vault)
    endpoint = await source_registry.create_endpoint(
        OWNER, name="workstation", base_url="http://source.invalid/v1"
    )
    await source_registry.set_role(OWNER, "main", [endpoint.id])
    envelope, _ = await source.export(OWNER, BACKUP_SECRET, include=["settings"])

    target_engine, target_vault, target = await _host(tmp_path, "target")
    target_registry = ModelRegistry(target_engine, target_vault)
    local = await target_registry.create_endpoint(
        OWNER, name="workstation", base_url="http://target.invalid/v1"
    )
    assert local.id != endpoint.id

    _ = await target.import_backup(OWNER, BACKUP_SECRET, envelope)

    with Session(target_engine) as session:
        role = session.exec(select(ModelRole).where(ModelRole.role == "main")).one()
    assert role.endpoint_ids == [local.id], "the chain must name an endpoint that exists here"
    # And it resolves — the reason the id matters at all.
    assert (await target_registry.get_endpoint(OWNER, role.endpoint_ids[0])).name == "workstation"
