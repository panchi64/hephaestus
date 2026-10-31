"""A plain directory standing in for the encrypted volume, for the suite only.

Mounting a real sparse bundle per booted test app would cost seconds each and needs
macOS; the real backends have their own tests (`test_volume.py`). Production has no
plaintext backend: `conftest` swaps this in, nothing else imports it.
"""

from __future__ import annotations

from pathlib import Path

from core.config import Settings


class DirectoryVolume:
    def __init__(self, mountpoint: Path) -> None:
        self._mountpoint: Path = mountpoint
        self._mounted: bool = False
        self.passphrases: list[str] = []

    @property
    def mountpoint(self) -> Path:
        return self._mountpoint

    def exists(self) -> bool:
        return self._mountpoint.exists() and bool(self.passphrases)

    def is_mounted(self) -> bool:
        return self._mounted

    async def create(self, passphrase: str, /) -> None:
        self.passphrases.append(passphrase)

    async def mount(self, passphrase: str, /) -> None:
        self.passphrases.append(passphrase)
        self._mounted = True

    async def unmount(self) -> None:
        self._mounted = False


def select_directory_volume(settings: Settings) -> DirectoryVolume:
    return DirectoryVolume(settings.volume_dir)
