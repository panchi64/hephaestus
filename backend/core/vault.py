"""The vault — the operator's encryption key, held only in memory.

On disk lives a small keyfile: the login verifier, the KEK salt, and the DEK
wrapped under the password-derived KEK. The **DEK itself is never written** — it
is unwrapped into memory at unlock and wiped at lock, so a restart leaves the
system locked and the data unreadable until the operator unlocks again. No OS
keystore is involved, which keeps this byte-for-byte identical across platforms.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from collections.abc import Callable
from pathlib import Path

from core import crypto

logger = logging.getLogger(__name__)

_KEYFILE_VERSION = 1
_KEYFILE_FIELDS = frozenset({"verifier", "kek_salt", "wrapped_dek"})


class VaultLocked(Exception):
    """Raised when encrypt/decrypt is attempted while the vault is locked."""


class VaultError(Exception):
    """Setup/unlock misuse (already initialized, not initialized, …)."""


def _b64e(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def _b64d(text: str) -> bytes:
    return base64.b64decode(text)


def _write_keyfile(path: Path, payload: dict[str, object]) -> None:
    """Write the keyfile atomically and owner-only.

    There is exactly one copy of the wrapped DEK, and nothing can reconstruct it: a
    torn write (crash, power loss, full disk) would take every encrypted row with it.
    So the bytes land in a sibling temp file that is flushed and fsynced, then renamed
    over the target — ``os.replace`` is atomic on POSIX, so a reader sees either the
    old file or the new one, never a truncated one. The directory entry is fsynced too,
    or the rename itself can be lost on crash.

    ``0o600`` before the rename, not after: on a shared host the default umask would
    otherwise leave the Argon2 verifier and the wrapped DEK world-readable for the
    window in between — enough to copy them and brute-force the password offline.
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


class Vault:
    def __init__(self, keyfile: Path) -> None:
        self._keyfile = keyfile
        self._dek: bytes | None = None
        self._unlocked = asyncio.Event()  # lets lock-aware workers wait on unlock
        # Callbacks other services register to react to a lock without the auth
        # route (or this class) knowing who they are — e.g. the operator shell
        # tearing down every live PTY session the instant the vault re-locks.
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
        salt = crypto.generate_salt()
        kek = await asyncio.to_thread(crypto.derive_kek, password, salt)
        # Argon2 by design burns CPU (64 MiB, t=3); on the event loop it would stall
        # every live SSE stream and tool loop for its full duration, so it threads like
        # `derive_kek` above.
        verifier = await asyncio.to_thread(crypto.hash_password, password)
        keyfile: dict[str, object] = {
            "version": _KEYFILE_VERSION,
            "verifier": verifier,
            "kek_salt": _b64e(salt),
            "wrapped_dek": _b64e(crypto.aead_encrypt(kek, dek)),
        }
        _write_keyfile(self._keyfile, keyfile)
        self._set_dek(dek)

    def _read_keyfile(self) -> dict[str, str]:
        """Parse the keyfile, turning any damage into `VaultError` rather than a 500.

        Writes are atomic, so a torn file shouldn't happen — but a hand-edited or
        externally-restored keyfile still reaches here, and a bare `JSONDecodeError`
        or `KeyError` out of the login route says nothing about what is wrong.
        """
        try:
            data = json.loads(self._keyfile.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise VaultError(f"keyfile is unreadable or corrupt: {exc}") from exc
        if not isinstance(data, dict) or not _KEYFILE_FIELDS <= data.keys():
            raise VaultError("keyfile is missing required fields")
        return data

    async def unlock(self, password: str) -> bool:
        """Verify the password and unwrap the DEK into memory. False on bad password."""
        if not self.is_initialized:
            raise VaultError("vault not initialized")
        data = self._read_keyfile()
        # Threaded for the same reason `derive_kek` is: the verify is the same Argon2
        # cost, and a login must not freeze the loop for every other connection.
        if not await asyncio.to_thread(crypto.verify_password, data["verifier"], password):
            return False
        kek = await asyncio.to_thread(crypto.derive_kek, password, _b64d(data["kek_salt"]))
        try:
            dek = crypto.aead_decrypt(kek, _b64d(data["wrapped_dek"]))
        except Exception:  # noqa: BLE001 — any unwrap failure is just a failed unlock
            return False
        self._set_dek(dek)
        await self._rehash_verifier_if_stale(data, password)
        return True

    async def _rehash_verifier_if_stale(self, data: dict[str, str], password: str) -> None:
        """Re-mint the login verifier when it predates the current Argon2 parameters.

        Only the verifier moves: `kek_salt` and `wrapped_dek` are copied through
        untouched, so a failure here can never cost the operator the DEK — it just
        leaves the old verifier in place until the next login.
        """
        if not await asyncio.to_thread(crypto.needs_rehash, data["verifier"]):
            return
        try:
            verifier = await asyncio.to_thread(crypto.hash_password, password)
            _write_keyfile(self._keyfile, {**data, "verifier": verifier})
        except Exception:  # noqa: BLE001 — a stale verifier still works; never fail a login
            logger.warning("could not re-mint the login verifier", exc_info=True)

    async def verify_password(self, password: str) -> bool:
        """Check the password against the stored verifier without unlocking —
        doesn't touch ``_dek``. Used by host-mode re-authentication, which needs
        proof of the password without granting/renewing decrypt access.

        Async because the Argon2 comparison is deliberately expensive; run inline it
        would freeze the event loop (and every live stream on it) for its duration."""
        if not self.is_initialized:
            return False
        data = self._read_keyfile()
        return await asyncio.to_thread(crypto.verify_password, data["verifier"], password)

    def register_on_lock(self, cb: Callable[[], None]) -> None:
        """Register a callback fired synchronously from `lock()`. Lets a
        capability (like the operator shell) react to a lock without this class
        knowing about it."""
        self._on_lock.append(cb)

    def lock(self) -> None:
        self._dek = None
        self._unlocked.clear()
        for cb in self._on_lock:
            try:
                cb()
            except Exception:  # noqa: BLE001 — one bad callback must not block the lock
                logger.exception("vault: on-lock callback failed")

    def encrypt_str(self, plaintext: str) -> str:
        return _b64e(crypto.aead_encrypt(self._require_dek(), plaintext.encode()))

    def decrypt_str(self, token: str) -> str:
        return crypto.aead_decrypt(self._require_dek(), _b64d(token)).decode()

    def encrypt_bytes(self, raw: bytes) -> bytes:
        """Seal a raw blob (e.g. a workspace archive) — returned bytes go to disk."""
        return crypto.aead_encrypt(self._require_dek(), raw)

    def decrypt_bytes(self, token: bytes) -> bytes:
        return crypto.aead_decrypt(self._require_dek(), token)

    def _set_dek(self, dek: bytes) -> None:
        self._dek = dek
        self._unlocked.set()

    def _require_dek(self) -> bytes:
        if self._dek is None:
            raise VaultLocked("vault is locked")
        return self._dek
