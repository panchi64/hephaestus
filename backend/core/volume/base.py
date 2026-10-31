"""The encrypted volume: user data that has to be real files, encrypted on disk.

The agent's scratch, staged plugin source, the browser profile and temp files are read
by tools that need a filesystem (the VM mounts scratch live), so they live under a
mounted volume whose backing store is ciphertext. It mounts at unlock with a passphrase
derived from the vault DEK and unmounts at lock. Nothing ever falls back to a plaintext
directory: without a backend the workspace refuses to open.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Protocol

from core.exceptions import HephaestusError

logger = logging.getLogger(__name__)

_TOOL_TIMEOUT_S = 120.0


class VolumeUnavailable(HephaestusError):
    """No encrypted-volume backend works on this host; the reason says what is missing."""


class VolumeError(HephaestusError):
    """A volume command failed: a wrong passphrase, a busy mount, a damaged image."""


class EncryptedVolume(Protocol):
    @property
    def mountpoint(self) -> Path: ...

    def exists(self) -> bool:
        """Whether the backing store has been created."""
        ...

    async def create(self, passphrase: str, /) -> None: ...

    async def mount(self, passphrase: str, /) -> None: ...

    async def unmount(self) -> None: ...

    def is_mounted(self) -> bool: ...


def is_mountpoint(path: Path) -> bool:
    return path.exists() and os.path.ismount(path)


async def run_tool(argv: list[str], *, stdin: bytes | None = None) -> None:
    """Run a volume tool, passing any secret over stdin; raise `VolumeError` on failure."""
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(stdin), _TOOL_TIMEOUT_S)
    except TimeoutError:
        proc.kill()
        _ = await proc.wait()
        raise VolumeError(f"{argv[0]} {argv[1]} timed out") from None
    if proc.returncode != 0:
        detail = stderr.decode(errors="replace").strip() or f"exit {proc.returncode}"
        raise VolumeError(f"{argv[0]} {argv[1]} failed: {detail}")


async def open_volume(volume: EncryptedVolume, key: bytes) -> None:
    """Create the volume on first use, then mount it owner-only.

    A mount left by a crashed process is unmounted first, so the key is proven on every
    open instead of trusting whatever is already there.
    """
    passphrase = key.hex()
    if volume.is_mounted():
        logger.warning("volume: unmounting a stale mount at %s", volume.mountpoint)
        await volume.unmount()
    volume.mountpoint.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not volume.exists():
        logger.info("volume: creating the encrypted volume")
        await volume.create(passphrase)
    await volume.mount(passphrase)
    volume.mountpoint.chmod(0o700)


async def close_volume(volume: EncryptedVolume) -> None:
    if volume.is_mounted():
        await volume.unmount()
