"""Moving a workspace written before encryption at rest onto it, once, at the first open.

Runs after the volume mounts and before the engine is built. Every step is idempotent and
safe to interrupt: a crash part-way leaves either the old file or a verified new one in
place, and the next open finishes the job. Only paths, counts and sizes are logged.

The database is re-encrypted with ``sqlcipher_export`` into a sibling file, verified (page
integrity, HMACs, per-table row counts), then swapped in. File trees that must stay files
move into the volume. The plaintext originals, and the old plaintext backups beside the
database, are then overwritten and unlinked.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import cast

from sqlcipher3 import dbapi2 as sqlcipher

from core.config import Settings
from core.db import key_pragma
from core.exceptions import HephaestusError

logger = logging.getLogger(__name__)

_SQLITE_HEADER = b"SQLite format 3\x00"
_SIDECARS = ("-wal", "-shm", "-journal")
_CHUNK = 1 << 20


class MigrationError(HephaestusError):
    """The encrypted copy did not verify; the plaintext original is left untouched."""


def migrate_legacy_plaintext(settings: Settings, db_key: bytes) -> None:
    """Encrypt a plaintext database, move plaintext files into the volume, shred the rest."""
    if settings.db_url is not None:
        # Not ours to move or delete: an explicit URL may point anywhere.
        return
    db_path = settings.data_dir / "app.db"
    _recover_interrupted_swap(db_path, db_key)
    if db_path.exists() and is_plaintext_sqlite(db_path):
        _encrypt_database(db_path, db_key)
    for source, target in _legacy_moves(settings):
        _move_into_volume(source, target)
    # Only once the live database is known to be the encrypted one.
    if db_path.exists() and not is_plaintext_sqlite(db_path):
        for leftover in _plaintext_leftovers(settings):
            shred_tree(leftover)


def is_plaintext_sqlite(path: Path) -> bool:
    """Whether ``path`` starts with SQLite's cleartext header (an encrypted file never does)."""
    with path.open("rb") as handle:
        return handle.read(len(_SQLITE_HEADER)) == _SQLITE_HEADER


def _staging(db_path: Path) -> Path:
    return db_path.with_name(f"{db_path.name}.enc-tmp")


def _retired(db_path: Path) -> Path:
    return db_path.with_name(f"{db_path.name}.plain-old")


def _encrypt_database(db_path: Path, key: bytes) -> None:
    staging, retired = _staging(db_path), _retired(db_path)
    staging.unlink(missing_ok=True)
    logger.info("at-rest: encrypting the plaintext database")
    source = sqlcipher.connect(str(db_path))
    try:
        # Folds any leftover WAL into the main file, so the export sees every row.
        _ = source.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
        version = int(str(_scalar(source, "PRAGMA user_version")))
        counts = _row_counts(source)
        quoted = str(staging).replace("'", "''")
        _ = source.execute(f"ATTACH DATABASE '{quoted}' AS enc KEY \"x'{key.hex()}'\"")
        _ = source.execute("SELECT sqlcipher_export('enc')")
        _ = source.execute(f"PRAGMA enc.user_version = {version}")
        _ = source.execute("DETACH DATABASE enc")
    finally:
        source.close()

    if not _verifies(staging, key, counts):
        staging.unlink(missing_ok=True)
        raise MigrationError("the encrypted copy of the database did not verify")
    _fsync(staging)
    for suffix in _SIDECARS:
        shred(db_path.with_name(db_path.name + suffix))
    os.replace(db_path, retired)
    os.replace(staging, db_path)
    _fsync(db_path.parent)
    shred(retired)
    logger.info("at-rest: database encrypted (%d tables verified)", len(counts))


def _recover_interrupted_swap(db_path: Path, key: bytes) -> None:
    """Finish or undo a swap a crash interrupted between its two renames."""
    staging, retired = _staging(db_path), _retired(db_path)
    if not db_path.exists() and retired.exists():
        if staging.exists() and _verifies(staging, key, None):
            os.replace(staging, db_path)
        else:
            os.replace(retired, db_path)
        _fsync(db_path.parent)
    if db_path.exists() and not is_plaintext_sqlite(db_path):
        shred(retired)
    staging.unlink(missing_ok=True)


def _scalar(connection: sqlcipher.Connection, sql: str) -> object:
    row = cast(tuple[object, ...], connection.execute(sql).fetchone())
    return row[0]


def _row_counts(connection: sqlcipher.Connection) -> dict[str, int]:
    rows = cast(
        list[tuple[str]],
        connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall(),
    )
    return {
        name: int(str(_scalar(connection, f'SELECT count(*) FROM "{name}"'))) for (name,) in rows
    }


def _verifies(path: Path, key: bytes, expected: dict[str, int] | None) -> bool:
    try:
        connection = sqlcipher.connect(str(path))
        try:
            _ = connection.execute(key_pragma(key))
            if _scalar(connection, "PRAGMA integrity_check") != "ok":
                return False
            if connection.execute("PRAGMA cipher_integrity_check").fetchall():
                return False
            return expected is None or _row_counts(connection) == expected
        finally:
            connection.close()
    except sqlcipher.DatabaseError:
        return False


def _legacy_moves(settings: Settings) -> list[tuple[Path, Path]]:
    data = settings.data_dir
    return [
        (data / "sandbox" / "work", settings.scratch_dir),
        (data / "plugins" / "installed", settings.plugins_install_dir),
        (data / "browser" / "profile.json", settings.browser_control_state_dir / "profile.json"),
        (data / "searxng" / "secret_key", settings.volume_dir / "searxng" / "secret_key"),
    ]


def _plaintext_leftovers(settings: Settings) -> Iterator[Path]:
    data = settings.data_dir
    yield from sorted(data.glob("app.db.pre-*"))
    yield data / "sandbox" / "sealed"
    # Whatever is left in the old browser state dir once the profile has moved.
    yield data / "browser"


def _move_into_volume(source: Path, target: Path) -> None:
    """Copy ``source`` into the volume, verify each file, then shred the original."""
    if not source.exists() and not source.is_symlink():
        return
    files = 0
    for path in _walk(source):
        destination = target / path.relative_to(source) if source.is_dir() else target
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if path.is_symlink():
            if not destination.is_symlink() and not destination.exists():
                destination.symlink_to(os.readlink(path))
        # An existing copy may be a crash's partial one, so it is checked, never trusted.
        elif not destination.exists() or _digest(destination) != _digest(path):
            _ = shutil.copy2(path, destination)
            _fsync(destination)
            if _digest(destination) != _digest(path):
                raise MigrationError(f"a copy into the volume did not verify: {destination}")
        files += 1
    shred_tree(source)
    logger.info("at-rest: moved %d file(s) from %s into the volume", files, source)


def _walk(root: Path) -> Iterator[Path]:
    if not root.is_dir() or root.is_symlink():
        yield root
        return
    # os.walk lists a symlinked directory under `dirs` without descending into it.
    for directory, dirs, names in os.walk(root):
        for name in names:
            yield Path(directory) / name
        for name in dirs:
            if (Path(directory) / name).is_symlink():
                yield Path(directory) / name


def shred(path: Path) -> None:
    """Overwrite a file with random bytes, fsync it, then unlink it.

    On copy-on-write filesystems (APFS, btrfs) and SSDs the overwrite may land on new
    blocks, so this is not a guaranteed wipe; full-disk encryption covers that remainder.
    """
    if path.is_symlink():
        path.unlink()
        return
    if not path.is_file():
        return
    size = path.stat().st_size
    with path.open("r+b") as handle:
        written = 0
        while written < size:
            step = min(_CHUNK, size - written)
            _ = handle.write(os.urandom(step))
            written += step
        handle.flush()
        os.fsync(handle.fileno())
    path.unlink()


def shred_tree(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        for child in _walk(path):
            shred(child)
        shutil.rmtree(path, ignore_errors=True)
    else:
        shred(path)


def _digest(path: Path) -> str:
    sha = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(_CHUNK):
            sha.update(block)
    return sha.hexdigest()


def _fsync(path: Path) -> None:
    """Flush a file, or a directory's entries after a rename."""
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
