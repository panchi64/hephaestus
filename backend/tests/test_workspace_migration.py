"""Moving a pre-encryption workspace onto encryption at rest, on synthetic data in tmp dirs."""

from __future__ import annotations

import os
from pathlib import Path
from typing import cast

import pytest
from sqlcipher3 import dbapi2 as sqlcipher

from core.config import Settings
from core.db import key_pragma
from harness import workspace_migration
from harness.workspace_migration import (
    MigrationError,
    is_plaintext_sqlite,
    migrate_legacy_plaintext,
    shred,
)

_KEY = bytes(range(32))


def _settings(data_dir: Path) -> Settings:
    return Settings(data_dir=data_dir, db_url=None)


def _plaintext_db(path: Path, rows: int = 3) -> None:
    connection = sqlcipher.connect(str(path))
    _ = connection.execute("CREATE TABLE note (body TEXT)")
    _ = connection.executemany(
        "INSERT INTO note VALUES (?)", [(f"PLAIN-ROW-{i}",) for i in range(rows)]
    )
    _ = connection.execute("PRAGMA user_version = 7")
    connection.commit()
    connection.close()


def _read_encrypted(path: Path) -> tuple[list[str], int]:
    connection = sqlcipher.connect(str(path))
    try:
        _ = connection.execute(key_pragma(_KEY))
        rows = cast(list[tuple[str]], connection.execute("SELECT body FROM note").fetchall())
        (version,) = cast(tuple[int], connection.execute("PRAGMA user_version").fetchone())
        return [body for (body,) in rows], version
    finally:
        connection.close()


def test_a_plaintext_database_is_encrypted_in_place_and_the_original_shredded(tmp_path: Path):
    settings = _settings(tmp_path)
    db = tmp_path / "app.db"
    _plaintext_db(db)
    assert is_plaintext_sqlite(db)

    migrate_legacy_plaintext(settings, _KEY)

    assert not is_plaintext_sqlite(db)
    assert b"PLAIN-ROW" not in db.read_bytes()
    assert _read_encrypted(db) == (["PLAIN-ROW-0", "PLAIN-ROW-1", "PLAIN-ROW-2"], 7)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["app.db"]

    migrate_legacy_plaintext(settings, _KEY)  # a second open changes nothing
    assert _read_encrypted(db)[0] == ["PLAIN-ROW-0", "PLAIN-ROW-1", "PLAIN-ROW-2"]


def test_a_copy_that_does_not_verify_leaves_the_plaintext_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    db = tmp_path / "app.db"
    _plaintext_db(db)

    def never(_path: Path, _key: bytes, _expected: object) -> bool:
        return False

    monkeypatch.setattr(workspace_migration, "_verifies", never)
    with pytest.raises(MigrationError):
        migrate_legacy_plaintext(_settings(tmp_path), _KEY)
    assert is_plaintext_sqlite(db)
    assert not (tmp_path / "app.db.enc-tmp").exists()


@pytest.mark.parametrize("staged", [True, False])
def test_a_crash_between_the_renames_is_finished_or_undone(tmp_path: Path, staged: bool):
    db = tmp_path / "app.db"
    _plaintext_db(db)
    migrate_legacy_plaintext(_settings(tmp_path), _KEY)
    encrypted = db.read_bytes()

    # Recreate the window between "original retired" and "copy promoted".
    retired, staging = tmp_path / "app.db.plain-old", tmp_path / "app.db.enc-tmp"
    _plaintext_db(retired)
    if staged:
        _ = staging.write_bytes(encrypted)
    db.unlink()

    migrate_legacy_plaintext(_settings(tmp_path), _KEY)
    # A verified staged copy is promoted; without one the original comes back and is
    # then encrypted like any plaintext database.
    assert not is_plaintext_sqlite(db)
    assert _read_encrypted(db)[0] == ["PLAIN-ROW-0", "PLAIN-ROW-1", "PLAIN-ROW-2"]
    assert not retired.exists() and not staging.exists()


def test_plaintext_files_move_into_the_volume_and_leftovers_are_shredded(tmp_path: Path):
    settings = _settings(tmp_path)
    _plaintext_db(tmp_path / "app.db")
    scratch = tmp_path / "sandbox" / "work" / "conv-1"
    scratch.mkdir(parents=True)
    _ = (scratch / "notes.md").write_text("agent scratch")
    (scratch / "link").symlink_to("notes.md")
    plugin = tmp_path / "plugins" / "installed" / "demo"
    plugin.mkdir(parents=True)
    _ = (plugin / "main.py").write_text("print('hi')")
    browser = tmp_path / "browser"
    browser.mkdir()
    _ = (browser / "profile.json").write_text('{"cookies": []}')
    _ = (browser / "0123abcd.json").write_text("{}")
    (tmp_path / "searxng").mkdir()
    _ = (tmp_path / "searxng" / "secret_key").write_text("k" * 64)
    _ = (tmp_path / "app.db.pre-modes-backup").write_bytes(b"old backup")
    sealed = tmp_path / "sandbox" / "sealed"
    sealed.mkdir(parents=True)
    _ = (sealed / "a.tar.enc.gz").write_bytes(b"archive")

    migrate_legacy_plaintext(settings, _KEY)

    moved = settings.scratch_dir / "conv-1"
    assert (moved / "notes.md").read_text() == "agent scratch"
    assert os.readlink(moved / "link") == "notes.md"
    assert (settings.plugins_install_dir / "demo" / "main.py").read_text() == "print('hi')"
    assert (settings.browser_control_state_dir / "profile.json").read_text() == '{"cookies": []}'
    assert (settings.volume_dir / "searxng" / "secret_key").read_text() == "k" * 64
    for gone in (
        tmp_path / "sandbox" / "work",
        tmp_path / "plugins" / "installed",
        browser,
        tmp_path / "searxng" / "secret_key",
        tmp_path / "app.db.pre-modes-backup",
        sealed,
        tmp_path / "sandbox",
        tmp_path / "plugins",
        tmp_path / "searxng",
    ):
        assert not gone.exists(), gone


def test_leftovers_stay_while_the_database_is_still_plaintext(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    db = tmp_path / "app.db"
    _plaintext_db(db)
    backup = tmp_path / "app.db.pre-d54-backup"
    _ = backup.write_bytes(b"old backup")

    def never(_path: Path, _key: bytes, _expected: object) -> bool:
        return False

    monkeypatch.setattr(workspace_migration, "_verifies", never)
    with pytest.raises(MigrationError):
        migrate_legacy_plaintext(_settings(tmp_path), _KEY)
    assert backup.exists()


def test_a_database_url_elsewhere_is_never_touched(tmp_path: Path):
    db = tmp_path / "elsewhere.db"
    _plaintext_db(db)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    migrate_legacy_plaintext(Settings(data_dir=data_dir, db_url=f"sqlite:///{db}"), _KEY)
    assert is_plaintext_sqlite(db)


def test_a_database_url_naming_the_default_file_is_still_migrated(tmp_path: Path):
    db = tmp_path / "app.db"
    _plaintext_db(db)
    migrate_legacy_plaintext(Settings(data_dir=tmp_path, db_url=f"sqlite:///{db}"), _KEY)
    assert not is_plaintext_sqlite(db)


def test_shred_overwrites_before_unlinking(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    target = tmp_path / "secret.bin"
    _ = target.write_bytes(b"S" * 5000)
    seen: list[bytes] = []
    real_unlink = Path.unlink

    def capture(self: Path, missing_ok: bool = False) -> None:
        seen.append(self.read_bytes())
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", capture)
    shred(target)
    assert not target.exists()
    assert len(seen[0]) == 5000 and seen[0] != b"S" * 5000
