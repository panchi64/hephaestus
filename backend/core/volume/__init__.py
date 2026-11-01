"""The encrypted volume for file-shaped user data; see `core.volume.base`."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from core.config import Settings
from core.volume.base import (
    EncryptedVolume,
    VolumeError,
    VolumeUnavailable,
    close_volume,
    open_volume,
)
from core.volume.linux import GocryptfsVolume, fusermount_binary
from core.volume.macos import HdiutilVolume

__all__ = [
    "EncryptedVolume",
    "UnavailableVolume",
    "VolumeError",
    "VolumeUnavailable",
    "backing_paths",
    "close_volume",
    "open_volume",
    "select_volume",
]


class UnavailableVolume:
    """Stands in when no backend works, so the reason reaches the unlock screen."""

    def __init__(self, mountpoint: Path, reason: str) -> None:
        self._mountpoint: Path = mountpoint
        self.reason: str = reason

    @property
    def mountpoint(self) -> Path:
        return self._mountpoint

    def exists(self) -> bool:
        return False

    def is_mounted(self) -> bool:
        return False

    async def create(self, _passphrase: str) -> None:
        raise VolumeUnavailable(self.reason)

    async def mount(self, _passphrase: str) -> None:
        raise VolumeUnavailable(self.reason)

    async def unmount(self) -> None:
        return None


def backing_paths(data_dir: Path) -> tuple[Path, ...]:
    """Every backing store a backend may keep under ``data_dir``, for reset and the fence."""
    return (data_dir / "volume.sparsebundle", data_dir / "volume.gocryptfs")


def select_volume(settings: Settings) -> EncryptedVolume:
    mountpoint = settings.volume_dir
    max_size = settings.volume_max_size
    sparsebundle, cipherdir = backing_paths(settings.data_dir)
    if sys.platform == "darwin":
        if shutil.which("hdiutil") is None:
            return UnavailableVolume(mountpoint, "hdiutil is not on PATH")
        return HdiutilVolume(sparsebundle, mountpoint, max_size=max_size)
    if sys.platform.startswith("linux"):
        fusermount = fusermount_binary()
        if shutil.which("gocryptfs") is None or fusermount is None:
            return UnavailableVolume(
                mountpoint, "install gocryptfs and fuse3 to keep files encrypted at rest"
            )
        return GocryptfsVolume(cipherdir, mountpoint, fusermount=fusermount)
    return UnavailableVolume(mountpoint, f"no encrypted volume backend for {sys.platform}")
