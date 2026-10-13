"""`BACKUP-1`: one file, encrypted under a separate secret, readable on another host."""

from __future__ import annotations

import gzip
import json

import pytest

from core.db import init_db, make_engine
from core.exceptions import DegradedCapabilityError
from core.vault import Vault
from services.backup import BackupSecretError, BackupService, discover_entities, sections
from services.backup.envelope import open_envelope
from services.memory import MemoryStore
from services.settings_store import SettingsStore

OWNER = "operator"
LOGIN_PASSWORD = "login-password"
BACKUP_SECRET = "correct horse battery staple"


async def _host(tmp_path, name: str = "host"):
    """A whole standalone install: its own DB, its own login vault, its own key."""
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    vault = Vault(tmp_path / f"{name}-keyfile.json")
    await vault.setup(f"{name}-{LOGIN_PASSWORD}")
    return engine, vault, BackupService(engine, vault, SettingsStore(engine))


class _NoEmbedder:
    """Memories with no vector — the backup's job is the rows, not the embeddings, and a
    vector re-derives from the content on whichever host imports it."""

    async def is_available(self, owner_id: str) -> bool:
        return False

    async def embed(self, owner_id: str, texts: list[str]):
        raise DegradedCapabilityError("no embedding endpoint configured")


def _memories(engine, vault) -> MemoryStore:
    return MemoryStore(engine, vault, _NoEmbedder())


async def _seed(engine, vault) -> MemoryStore:
    store = _memories(engine, vault)
    await store.remember(OWNER, "The operator's landlord is named Ferdinand")
    await store.remember(OWNER, "Deploys go out on Thursdays", pinned=True)
    return store


async def test_the_exported_file_carries_no_readable_user_data(tmp_path):
    engine, vault, backup = await _host(tmp_path)
    await _seed(engine, vault)

    envelope, manifest = await backup.export(OWNER, BACKUP_SECRET)

    # The whole file, as it would be written to disk.
    on_disk = json.dumps(envelope).encode()
    assert b"Ferdinand" not in on_disk
    assert b"Thursdays" not in on_disk
    # Nor is any of it accidentally readable as text anywhere in the payload.
    assert b"memories" not in on_disk

    # Only bookkeeping is in the clear: a format, a version, a salt, an algorithm.
    assert envelope["format"] == "hephaestus-backup"
    assert set(envelope) == {"format", "version", "created_at", "kdf", "cipher", "payload"}

    assert dict((i.name, i.count) for i in manifest.items)["memories"] == 2


async def test_the_backup_secret_alone_opens_it_not_the_login_password(tmp_path):
    engine, vault, backup = await _host(tmp_path)
    await _seed(engine, vault)
    envelope, _ = await backup.export(OWNER, BACKUP_SECRET)

    # The login password is neither needed nor sufficient — this is what makes a backup
    # restorable on a host that has never seen this operator's login.
    with pytest.raises(BackupSecretError):
        open_envelope(f"host-{LOGIN_PASSWORD}", envelope)
    with pytest.raises(BackupSecretError):
        open_envelope("not-the-secret", envelope)

    payload = json.loads(gzip.decompress(open_envelope(BACKUP_SECRET, envelope)))
    contents = json.dumps(payload)
    assert "Ferdinand" in contents  # decrypted, plainly, with the backup secret alone


async def test_a_tampered_header_fails_to_open(tmp_path):
    engine, vault, backup = await _host(tmp_path)
    await _seed(engine, vault)
    envelope, _ = await backup.export(OWNER, BACKUP_SECRET)

    # The header is bound into the ciphertext, so editing it is not a silent success.
    tampered = {**envelope, "created_at": "1999-01-01T00:00:00+00:00"}
    with pytest.raises(BackupSecretError):
        open_envelope(BACKUP_SECRET, tampered)


async def test_it_decrypts_and_restores_on_a_completely_different_host(tmp_path):
    source_engine, source_vault, source = await _host(tmp_path, "source")
    await _seed(source_engine, source_vault)
    envelope, _ = await source.export(OWNER, BACKUP_SECRET)

    # A second install: different DB, different login password, different DEK.
    target_engine, target_vault, target = await _host(tmp_path, "target")
    report = await target.import_backup(OWNER, BACKUP_SECRET, envelope)
    assert report.imported["memories"] == 2

    restored = await _memories(target_engine, target_vault).list_memories(OWNER)
    assert {m.content for m in restored} == {
        "The operator's landlord is named Ferdinand",
        "Deploys go out on Thursdays",
    }
    assert [m.pinned for m in sorted(restored, key=lambda m: m.content)] == [True, False]


async def test_include_selects_groups(tmp_path):
    engine, vault, backup = await _host(tmp_path)
    await _seed(engine, vault)

    envelope, manifest = await backup.export(OWNER, BACKUP_SECRET, include=["skills"])
    assert [i.name for i in manifest.items] == ["skills"]

    payload = json.loads(gzip.decompress(open_envelope(BACKUP_SECRET, envelope)))
    assert set(payload["sections"]) == {"skills"}


async def test_the_streamed_document_has_the_same_shape_every_entity_is_present(tmp_path):
    # The payload is written straight into the gzip stream one entity at a time rather than
    # assembled as a dict and dumped — which is only safe if the punctuation comes out
    # right. Every marked entity must appear under its section, including the ones that
    # exported no rows, because the importer looks tables up by name.
    engine, vault, backup = await _host(tmp_path)
    await _seed(engine, vault)

    envelope, _ = await backup.export(OWNER, BACKUP_SECRET)
    payload = json.loads(gzip.decompress(open_envelope(BACKUP_SECRET, envelope)))

    assert set(payload) == {"created_at", "sections"}
    expected: dict[str, set[str]] = {}
    for entity in discover_entities():
        expected.setdefault(entity.spec.section, set()).add(entity.name)
    assert {s: set(tables) for s, tables in payload["sections"].items()} == expected
    assert all(isinstance(rows, list) for t in payload["sections"].values() for rows in t.values())


async def test_the_gzip_header_carries_no_second_timestamp(tmp_path):
    # The document already stamps its own `created_at`; the container should not stamp a
    # different one beside it, where nothing reads it and everything has to ignore it.
    engine, vault, backup = await _host(tmp_path)
    await _seed(engine, vault)

    envelope, _ = await backup.export(OWNER, BACKUP_SECRET)

    raw = open_envelope(BACKUP_SECRET, envelope)
    assert raw[:2] == b"\x1f\x8b"  # gzip magic
    assert raw[4:8] == b"\x00\x00\x00\x00"  # MTIME field, zeroed


async def test_counting_a_group_does_not_read_its_rows(tmp_path):
    # The backup screen's readout is a count, not an export: it must not load (and decrypt)
    # every row the operator owns just to call `len` on the result.
    engine, vault, backup = await _host(tmp_path)
    await _seed(engine, vault)
    loaded: list[str] = []
    original = backup._rows

    async def counted(entity, owner_id):
        loaded.append(entity.name)
        return await original(entity, owner_id)

    backup._rows = counted

    counts = {item.name: item.count for item in await backup.counts(OWNER)}

    assert counts["memories"] == 2
    assert loaded == []


async def test_the_manifest_is_discovered_not_hardcoded():
    # Every group the export offers traces back to a `__backup__` marker on some entity.
    marked = {entity.spec.section for entity in discover_entities()}
    assert set(sections()) == marked
    assert {"memories", "skills", "settings", "preferences"} <= marked

    # The secrets manager is deliberately excluded: it has its own lock, and folding it
    # into a file protected by one backup secret would undo that.
    assert not any(e.model.__name__.startswith("Secret") for e in discover_entities())


async def test_the_last_export_is_remembered_and_absent_before_the_first(tmp_path):
    _, _, backup = await _host(tmp_path)
    assert await backup.last_manifest(OWNER) is None

    _, manifest = await backup.export(OWNER, BACKUP_SECRET)
    remembered = await backup.last_manifest(OWNER)
    assert remembered is not None
    assert remembered.created_at == manifest.created_at
    assert remembered.items == manifest.items
