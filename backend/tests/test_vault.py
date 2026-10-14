"""Crypto primitives and the vault: key derivation, wrap/unwrap, lock/unlock."""

from __future__ import annotations

from pathlib import Path

import pytest
from cryptography.exceptions import InvalidTag

from core import crypto
from core.vault import Vault, VaultError, VaultLocked

from ._json import obj, parse_json


def _always_stale(_hash: str) -> bool:
    return True


# --- primitives --------------------------------------------------------------
def test_aead_round_trip():
    key = crypto.generate_dek()
    blob = crypto.aead_encrypt(key, b"secret", b"aad")
    assert crypto.aead_decrypt(key, blob, b"aad") == b"secret"


def test_aead_rejects_tampering():
    key = crypto.generate_dek()
    blob = bytearray(crypto.aead_encrypt(key, b"secret"))
    blob[-1] ^= 0x01
    with pytest.raises(InvalidTag):
        _ = crypto.aead_decrypt(key, bytes(blob))


def test_password_hash_and_verify():
    h = crypto.hash_password("hunter2")
    assert crypto.verify_password(h, "hunter2")
    assert not crypto.verify_password(h, "wrong")


def test_kek_is_deterministic_per_salt():
    salt = crypto.generate_salt()
    assert crypto.derive_kek("pw", salt) == crypto.derive_kek("pw", salt)
    assert crypto.derive_kek("pw", salt) != crypto.derive_kek("pw", crypto.generate_salt())


# --- vault -------------------------------------------------------------------
async def test_setup_then_encrypt_decrypt(tmp_path: Path):
    vault = Vault(tmp_path / "keyfile.json")
    assert not vault.is_initialized
    await vault.setup("operator-pw")
    assert vault.is_initialized and vault.is_unlocked

    token = vault.encrypt_str("private note")
    assert token != "private note"
    assert vault.decrypt_str(token) == "private note"


async def test_locked_vault_cannot_encrypt(tmp_path: Path):
    vault = Vault(tmp_path / "keyfile.json")
    await vault.setup("pw")
    token = vault.encrypt_str("note")
    vault.lock()
    assert not vault.is_unlocked
    with pytest.raises(VaultLocked):
        _ = vault.decrypt_str(token)


async def test_unlock_round_trip_across_restart(tmp_path: Path):
    keyfile = tmp_path / "keyfile.json"
    vault = Vault(keyfile)
    await vault.setup("pw")
    token = vault.encrypt_str("note")

    # a fresh Vault over the same keyfile starts locked (restart)
    restarted = Vault(keyfile)
    assert restarted.is_initialized and not restarted.is_unlocked
    assert await restarted.unlock("wrong") is False
    assert await restarted.unlock("pw") is True
    assert restarted.decrypt_str(token) == "note"


async def test_setup_twice_and_unlock_uninitialized(tmp_path: Path):
    vault = Vault(tmp_path / "keyfile.json")
    await vault.setup("pw")
    with pytest.raises(VaultError):
        await vault.setup("pw")

    fresh = Vault(tmp_path / "other.json")
    with pytest.raises(VaultError):
        _ = await fresh.unlock("pw")


# --- keyfile integrity -------------------------------------------------------
async def test_keyfile_is_written_atomically_and_owner_only(tmp_path: Path):
    # The wrapped DEK has no second copy: a torn write is unrecoverable data loss,
    # and a world-readable one hands out the Argon2 verifier for offline cracking.
    path = tmp_path / "keyfile.json"
    vault = Vault(path)
    await vault.setup("pw")

    assert path.stat().st_mode & 0o077 == 0
    assert not list(tmp_path.glob("*.tmp*"))  # no scratch file left behind


@pytest.mark.parametrize("damage", ["not json at all", "{}", '{"verifier": "only"}'])
async def test_corrupt_keyfile_raises_vault_error_not_a_bare_crash(tmp_path: Path, damage: str):
    # A hand-edited or externally-restored keyfile must surface as VaultError, not as
    # a JSONDecodeError/KeyError escaping the login route as a 500.
    path = tmp_path / "keyfile.json"
    vault = Vault(path)
    await vault.setup("pw")
    _ = path.write_text(damage)

    with pytest.raises(VaultError):
        _ = await Vault(path).unlock("pw")


async def test_unlock_rehashes_a_verifier_minted_under_weaker_parameters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # Raising the work factors is worthless if existing verifiers never move, so a
    # successful unlock re-mints a stale one — leaving the DEK material untouched.
    path = tmp_path / "keyfile.json"
    vault = Vault(path)
    await vault.setup("pw")
    before = obj(parse_json(path.read_text()))

    monkeypatch.setattr(crypto, "needs_rehash", _always_stale)
    assert await Vault(path).unlock("pw") is True

    after = obj(parse_json(path.read_text()))
    assert after["verifier"] != before["verifier"]
    # Only the verifier moves: the salt and the wrapped DEK are copied through.
    assert after["kek_salt"] == before["kek_salt"]
    assert after["wrapped_dek"] == before["wrapped_dek"]
    assert await Vault(path).unlock("pw") is True


async def test_a_failing_rehash_never_fails_the_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    path = tmp_path / "keyfile.json"
    vault = Vault(path)
    await vault.setup("pw")

    def failing_hash(_pw: str) -> str:
        raise RuntimeError("no entropy")

    monkeypatch.setattr(crypto, "needs_rehash", _always_stale)
    monkeypatch.setattr(crypto, "hash_password", failing_hash)
    assert await Vault(path).unlock("pw") is True


def test_verify_password_rejects_a_malformed_hash_instead_of_raising():
    # `VerificationError` is the parent of `VerifyMismatchError`; catching only the
    # mismatch let every other verification failure escape as a 500.
    assert crypto.verify_password("not-a-phc-string", "pw") is False
    assert crypto.needs_rehash("not-a-phc-string") is False
