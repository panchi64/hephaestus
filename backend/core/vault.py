"""The vault — the operator's encryption key, held only in memory.

On disk lives a small keyfile: the login verifier, the KEK salt and work factors,
and the DEK wrapped under the password-derived KEK. The **DEK itself is never
written** — it is unwrapped into memory at unlock and zeroed at lock, so a restart
leaves the system locked and the data unreadable until the operator unlocks again.
The database file and the encrypted volume are keyed by subkeys derived from the
DEK. No OS keystore is involved, which keeps this identical across platforms.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from collections.abc import Callable
from pathlib import Path
from typing import Literal, cast

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from core import crypto
from core.serde import parse_json

logger = logging.getLogger(__name__)

# v1 carried no work factors (they were `crypto.KDF_V1`); v2 records them.
_KEYFILE_VERSION = 2
_KEYFILE_FIELDS = frozenset({"verifier", "kek_salt", "wrapped_dek"})

SubkeyPurpose = Literal["db", "volume"]


class VaultLocked(Exception):
    """Raised when encrypt/decrypt is attempted while the vault is locked."""


class VaultError(Exception):
    """Setup/unlock misuse (already initialized, not initialized, …)."""


def _b64e(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def _b64d(text: str) -> bytes:
    return base64.b64decode(text)


def _write_keyfile(path: Path, payload: dict[str, object]) -> None:
    """Write the keyfile atomically (fsynced temp file, rename, fsynced dir) and owner-only.

    The wrapped DEK has no second copy, so a torn write would lose every encrypted byte.
    The mode is set at creation, not after the rename, so the verifier and wrapped DEK are
    never world-readable even briefly.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(payload, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        tmp.unlink(missing_ok=True)


def _kdf_params(data: dict[str, object]) -> crypto.KdfParams:
    if data.get("version", 1) == 1:
        return crypto.KDF_V1
    try:
        return crypto.KdfParams.from_json(data.get("kdf"))
    except ValueError as exc:
        raise VaultError(f"keyfile kdf parameters are invalid: {exc}") from exc


async def _wrap(password: str, dek: bytes | bytearray, verifier: str) -> dict[str, object]:
    """A v2 keyfile wrapping ``dek`` under a fresh salt and the current work factors."""
    salt = crypto.generate_salt()
    params = crypto.current_kdf()
    # Argon2 burns CPU by design; on the event loop it would stall every live stream.
    kek = await asyncio.to_thread(crypto.derive_kek, password, salt, params)
    return {
        "version": _KEYFILE_VERSION,
        "verifier": verifier,
        "kek_salt": _b64e(salt),
        "kdf": params.to_json(),
        "wrapped_dek": _b64e(crypto.aead_encrypt(kek, bytes(dek))),
    }


class Vault:
    def __init__(self, keyfile: Path) -> None:
        self._keyfile: Path = keyfile
        self._dek: bytearray | None = None
        self._cipher: AESGCM | None = None
        self._subkeys: dict[SubkeyPurpose, bytearray] = {}
        self._unlocked: asyncio.Event = asyncio.Event()  # lets lock-aware workers wait on unlock
        # Lets services (the operator shell, say) react to a lock without this class
        # knowing who they are.
        self._on_lock: list[Callable[[], None]] = []

    @property
    def is_initialized(self) -> bool:
        return self._keyfile.exists()

    @property
    def is_unlocked(self) -> bool:
        return self._dek is not None

    @property
    def unlocked_event(self) -> asyncio.Event:
        return self._unlocked

    async def setup(self, password: str) -> None:
        """First run: mint a DEK, wrap it under the password, write the keyfile."""
        if self.is_initialized:
            raise VaultError("vault already initialized")
        dek = crypto.generate_dek()
        verifier = await asyncio.to_thread(crypto.hash_password, password)
        _write_keyfile(self._keyfile, await _wrap(password, dek, verifier))
        self._set_dek(dek)

    def _read_keyfile(self) -> dict[str, object]:
        """Parse the keyfile, turning any damage into `VaultError` rather than a 500."""
        try:
            data = parse_json(self._keyfile.read_text())
        except (OSError, ValueError) as exc:
            raise VaultError(f"keyfile is unreadable or corrupt: {exc}") from exc
        if not isinstance(data, dict) or not _KEYFILE_FIELDS <= data.keys():
            raise VaultError("keyfile is missing required fields")
        data = cast("dict[str, object]", data)
        if not all(isinstance(data[field], str) for field in _KEYFILE_FIELDS):
            raise VaultError("keyfile fields must be strings")
        return data

    async def unlock(self, password: str) -> bool:
        """Verify the password and unwrap the DEK into memory. False on bad password."""
        if not self.is_initialized:
            raise VaultError("vault not initialized")
        data = self._read_keyfile()
        verifier = cast(str, data["verifier"])
        if not await asyncio.to_thread(crypto.verify_password, verifier, password):
            return False
        params = _kdf_params(data)
        salt = _b64d(cast(str, data["kek_salt"]))
        kek = await asyncio.to_thread(crypto.derive_kek, password, salt, params)
        try:
            dek = crypto.aead_decrypt(kek, _b64d(cast(str, data["wrapped_dek"])))
        except InvalidTag, ValueError:  # a failed unwrap is just a failed unlock
            return False
        self._set_dek(dek)
        await self._rewrap_if_stale(data, params, password)
        return True

    async def _rewrap_if_stale(
        self, data: dict[str, object], params: crypto.KdfParams, password: str
    ) -> None:
        """Move a keyfile minted under weaker work factors onto the current ones.

        The DEK itself never changes, so no data is re-encrypted, and the new keyfile
        replaces the old one atomically; a failure leaves the old one working.
        """
        verifier = cast(str, data["verifier"])
        stale_verifier = await asyncio.to_thread(crypto.needs_rehash, verifier)
        stale_kek = params != crypto.current_kdf() or data.get("version") != _KEYFILE_VERSION
        if not (stale_verifier or stale_kek):
            return
        try:
            if stale_verifier:
                verifier = await asyncio.to_thread(crypto.hash_password, password)
            if stale_kek:
                payload = await _wrap(password, self._require_dek(), verifier)
            else:
                payload = {**data, "verifier": verifier}
            _write_keyfile(self._keyfile, payload)
        except Exception:  # the old keyfile still unlocks; never fail a login
            logger.warning("vault: could not move the keyfile to current parameters", exc_info=True)

    async def verify_password(self, password: str) -> bool:
        """Check the password against the stored verifier without unlocking.

        Used by host-mode re-authentication, which needs proof of the password
        without granting or renewing decrypt access.
        """
        if not self.is_initialized:
            return False
        verifier = cast(str, self._read_keyfile()["verifier"])
        return await asyncio.to_thread(crypto.verify_password, verifier, password)

    def register_on_lock(self, cb: Callable[[], None]) -> None:
        """Register a callback fired synchronously from `lock()`."""
        self._on_lock.append(cb)

    def clear_lock_callbacks(self) -> None:
        """Drop every registered callback, once the units that registered them are gone."""
        self._on_lock.clear()

    def lock(self) -> None:
        if self._dek is not None:
            crypto.wipe(self._dek)
        for subkey in self._subkeys.values():
            crypto.wipe(subkey)
        self._dek = None
        self._cipher = None
        self._subkeys.clear()
        self._unlocked.clear()
        for cb in self._on_lock:
            try:
                cb()
            except Exception:  # one bad callback must not block the lock
                logger.exception("vault: on-lock callback failed")

    def subkey(self, purpose: SubkeyPurpose) -> bytes:
        """The key for one encryption layer (database file, volume), derived from the DEK."""
        dek = self._require_dek()
        if purpose not in self._subkeys:
            self._subkeys[purpose] = crypto.derive_subkey(dek, purpose)
        return bytes(self._subkeys[purpose])

    def encrypt_str(self, plaintext: str) -> str:
        return _b64e(crypto.aead_encrypt(self._require_cipher(), plaintext.encode()))

    def decrypt_str(self, token: str) -> str:
        return crypto.aead_decrypt(self._require_cipher(), _b64d(token)).decode()

    def encrypt_bytes(self, raw: bytes) -> bytes:
        return crypto.aead_encrypt(self._require_cipher(), raw)

    def decrypt_bytes(self, token: bytes) -> bytes:
        return crypto.aead_decrypt(self._require_cipher(), token)

    def _set_dek(self, dek: bytes) -> None:
        self._dek = bytearray(dek)
        self._cipher = AESGCM(dek)
        self._unlocked.set()

    def _require_dek(self) -> bytearray:
        if self._dek is None:
            raise VaultLocked("vault is locked")
        return self._dek

    def _require_cipher(self) -> AESGCM:
        if self._cipher is None:
            raise VaultLocked("vault is locked")
        return self._cipher
