"""Low-level cryptographic primitives for at-rest encryption.

Everything at rest is symmetric with 256-bit keys, so nothing depends on the
public-key schemes a quantum computer breaks; Grover only halves the strength,
leaving ~128-bit (the CNSA 2.0 bar). AES-256-GCM protects content. Argon2id
stretches the operator's password two independent ways — a login verifier and a
key-encryption key — so the stored login hash is useless for decryption.
HKDF-SHA512 splits the DEK into per-purpose subkeys (database file, volume), so
no two layers share key material.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import cast

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from argon2.low_level import Type, hash_secret_raw
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


@dataclass(frozen=True)
class KdfParams:
    """Argon2id work factors; recorded beside every wrapped key so old ones still open."""

    time_cost: int
    memory_cost: int  # KiB
    parallelism: int

    def to_json(self) -> dict[str, int]:
        return {"t": self.time_cost, "m": self.memory_cost, "p": self.parallelism}

    @classmethod
    def from_json(cls, value: object) -> KdfParams:
        if not isinstance(value, dict):
            raise ValueError("kdf parameters must be an object")
        fields = cast(dict[str, object], value)
        t, m, p = fields.get("t"), fields.get("m"), fields.get("p")
        if not (isinstance(t, int) and isinstance(m, int) and isinstance(p, int)):
            raise ValueError("kdf parameters must be integers")
        return cls(time_cost=t, memory_cost=m, parallelism=p)


# Every keyfile, password vault and backup written before keyfile v2 used these.
KDF_V1 = KdfParams(time_cost=3, memory_cost=65536, parallelism=4)
# 512 MiB over four passes: ~0.25 s on an M-series host, 8x the memory hardness of v1.
KDF_V2 = KdfParams(time_cost=4, memory_cost=524288, parallelism=4)
_KEK_LEN = 32

_NONCE_LEN = 12
_SALT_LEN = 16
_SUBKEY_LEN = 32

PASSWORD_HASHER = PasswordHasher(
    time_cost=KDF_V2.time_cost,
    memory_cost=KDF_V2.memory_cost,
    parallelism=KDF_V2.parallelism,
)


def current_kdf() -> KdfParams:
    # Read at call time, not bound as a default, so the suite's floor costs apply.
    return KDF_V2


def hash_password(password: str) -> str:
    """A verifier-only Argon2id hash (PHC string) for login checks."""
    return PASSWORD_HASHER.hash(password)


def verify_password(stored_hash: str, password: str) -> bool:
    # `VerificationError` is the parent of `VerifyMismatchError`: catching only the
    # mismatch leaves every other verification failure (a truncated or foreign-format
    # PHC string) escaping as a 500 out of the login route instead of a clean "no".
    try:
        return PASSWORD_HASHER.verify(stored_hash, password)
    except VerificationError, InvalidHashError:
        return False


def needs_rehash(stored_hash: str) -> bool:
    """Whether a verifier predates the current Argon2 work factors."""
    try:
        return PASSWORD_HASHER.check_needs_rehash(stored_hash)
    except VerificationError, InvalidHashError:
        return False


def derive_kek(password: str, salt: bytes, params: KdfParams) -> bytes:
    """Derive the 256-bit key-encryption key from the password (CPU-heavy)."""
    return hash_secret_raw(
        password.encode(),
        salt,
        time_cost=params.time_cost,
        memory_cost=params.memory_cost,
        parallelism=params.parallelism,
        hash_len=_KEK_LEN,
        type=Type.ID,
    )


def derive_subkey(root: bytes | bytearray, purpose: str) -> bytearray:
    """A 256-bit key for one purpose, independent of every other purpose's key."""
    hkdf = HKDF(
        algorithm=hashes.SHA512(),
        length=_SUBKEY_LEN,
        salt=None,
        info=f"hephaestus/{purpose}/v1".encode(),
    )
    return bytearray(hkdf.derive(bytes(root)))


def generate_dek() -> bytes:
    return AESGCM.generate_key(bit_length=256)


def generate_salt() -> bytes:
    return os.urandom(_SALT_LEN)


def wipe(buffer: bytearray) -> None:
    """Zero a key buffer in place. Best effort: copies made by C libraries are out of reach."""
    buffer[:] = bytes(len(buffer))


def aead_encrypt(key: bytes | AESGCM, plaintext: bytes, aad: bytes = b"") -> bytes:
    """AES-256-GCM; returns ``nonce || ciphertext`` (nonce is random per call)."""
    cipher = key if isinstance(key, AESGCM) else AESGCM(key)
    nonce = os.urandom(_NONCE_LEN)
    return nonce + cipher.encrypt(nonce, plaintext, aad)


def aead_decrypt(key: bytes | AESGCM, blob: bytes, aad: bytes = b"") -> bytes:
    cipher = key if isinstance(key, AESGCM) else AESGCM(key)
    nonce, ciphertext = blob[:_NONCE_LEN], blob[_NONCE_LEN:]
    return cipher.decrypt(nonce, ciphertext, aad)
