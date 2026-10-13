"""The backup file format — one file, encrypted under a **separate** backup secret.

`BACKUP-1` asks for a single file that carries no plainly-readable user data and that can be
decrypted on another host. The login-derived DEK cannot do that job: it never leaves this
machine and it is not something the operator can carry. So an export is sealed under a key
derived from a **recovery secret the operator supplies** — a passphrase or recovery key — and
that is the only thing needed to open it anywhere.

The file is JSON so it survives being mailed, copied, and stored anywhere text goes:

.. code-block:: json

    {"format": "hephaestus-backup", "version": 1, "created_at": "…",
     "kdf": {"algorithm": "argon2id-kek-v1", "salt": "…"},
     "cipher": "AES-256-GCM", "payload": "<base64 nonce||ciphertext>"}

Everything outside ``payload`` is format bookkeeping — a salt and two algorithm names — and
no part of it is user data. The header is bound into the ciphertext as AEAD associated data,
so editing it (pointing the salt somewhere else, say) fails the open rather than silently
decrypting something else.

Derivation reuses ``core.crypto`` rather than reaching for Argon2 here: one place in the tree
knows how to stretch a secret into a key, and the ``argon2id-kek-v1`` tag records *which*
scheme a given file used, so a future change to those parameters can still open old backups.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from core import crypto
from core.exceptions import HephaestusError

FORMAT = "hephaestus-backup"
VERSION = 1
# The derivation this file used. A tag, not parameters: it names the scheme in
# `core.crypto.derive_kek`, so a later change to the cost factors can branch on it instead of
# stranding every backup ever written.
KDF_ALGORITHM = "argon2id-kek-v1"
CIPHER = "AES-256-GCM"


class BackupFormatError(HephaestusError):
    """The file is not an Hephaestus backup, or is a version this build can't read."""


class BackupSecretError(HephaestusError):
    """The backup secret is wrong, or the file has been altered since it was written."""


def _header(envelope: Mapping[str, Any]) -> dict[str, Any]:
    """The bookkeeping half of the envelope — everything but the ciphertext. Serialized
    canonically below and bound into the AEAD, so it cannot be edited after the fact."""
    return {key: value for key, value in envelope.items() if key != "payload"}


def _aad(header: Mapping[str, Any]) -> bytes:
    return json.dumps(header, sort_keys=True, separators=(",", ":")).encode()


def seal(secret: str, payload: bytes, *, created_at: datetime | None = None) -> dict[str, Any]:
    """Wrap ``payload`` into a self-describing, encrypted envelope. CPU-heavy (Argon2id) —
    call it off the event loop."""
    if not secret:
        raise ValueError("a backup secret is required")
    salt = crypto.generate_salt()
    key = crypto.derive_kek(secret, salt)
    header = {
        "format": FORMAT,
        "version": VERSION,
        "created_at": (created_at or datetime.now(UTC)).isoformat(),
        "kdf": {"algorithm": KDF_ALGORITHM, "salt": base64.b64encode(salt).decode()},
        "cipher": CIPHER,
    }
    sealed = crypto.aead_encrypt(key, payload, _aad(header))
    return {**header, "payload": base64.b64encode(sealed).decode()}


def open_envelope(secret: str, envelope: Mapping[str, Any]) -> bytes:
    """Recover the payload. Raises :class:`BackupFormatError` for something that isn't one
    of our files, :class:`BackupSecretError` for a wrong secret or a tampered file — the two
    are worth telling apart, since only one of them is the operator's to fix by retyping."""
    if not secret:
        raise ValueError("a backup secret is required")
    if not isinstance(envelope, Mapping) or envelope.get("format") != FORMAT:
        raise BackupFormatError("not an Hephaestus backup file")
    if envelope.get("version") != VERSION:
        raise BackupFormatError(
            f"backup format version {envelope.get('version')!r} is not supported"
        )
    kdf = envelope.get("kdf")
    if not isinstance(kdf, Mapping) or kdf.get("algorithm") != KDF_ALGORITHM:
        raise BackupFormatError("unsupported key derivation in this backup file")
    try:
        salt = base64.b64decode(kdf["salt"])
        sealed = base64.b64decode(envelope["payload"])
    except (KeyError, TypeError, ValueError) as exc:
        raise BackupFormatError("the backup file is malformed") from exc

    key = crypto.derive_kek(secret, salt)
    try:
        return crypto.aead_decrypt(key, sealed, _aad(_header(envelope)))
    except Exception as exc:  # noqa: BLE001 — AEAD failure is one answer: it didn't open
        raise BackupSecretError(
            "the backup could not be decrypted — wrong secret, or the file was altered"
        ) from exc
