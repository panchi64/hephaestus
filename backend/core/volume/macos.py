"""macOS backend: an AES-256 encrypted APFS sparse bundle, built into the OS (`hdiutil`)."""

from __future__ import annotations

import logging
from pathlib import Path

from core.volume.base import VolumeError, is_mountpoint, run_tool

logger = logging.getLogger(__name__)


class HdiutilVolume:
    def __init__(self, image: Path, mountpoint: Path, *, max_size: str) -> None:
        self._image: Path = image
        self._mountpoint: Path = mountpoint
        self._max_size: str = max_size

    @property
    def mountpoint(self) -> Path:
        return self._mountpoint

    def exists(self) -> bool:
        return self._image.exists()

    def is_mounted(self) -> bool:
        return is_mountpoint(self._mountpoint)

    async def create(self, passphrase: str) -> None:
        # The bundle grows in bands as it fills; `max_size` only caps it.
        await run_tool(
            [
                "hdiutil", "create", "-quiet",
                "-type", "SPARSEBUNDLE", "-fs", "APFS",
                "-encryption", "AES-256", "-stdinpass",
                "-size", self._max_size, "-volname", "Hephaestus",
                str(self._image),
            ],
            stdin=_null_terminated(passphrase),
        )  # fmt: skip

    async def mount(self, passphrase: str) -> None:
        # `-nobrowse` keeps it out of Finder and Spotlight's sidebar; `-owners on` makes the
        # volume honour file ownership instead of handing everything to the console user.
        await run_tool(
            [
                "hdiutil", "attach", "-quiet", "-stdinpass",
                "-nobrowse", "-noautoopen", "-owners", "on",
                "-mountpoint", str(self._mountpoint),
                str(self._image),
            ],
            stdin=_null_terminated(passphrase),
        )  # fmt: skip

    async def unmount(self) -> None:
        try:
            await run_tool(["hdiutil", "detach", "-quiet", str(self._mountpoint)])
        except VolumeError:
            logger.warning("volume: detach refused, forcing it", exc_info=True)
            await run_tool(["hdiutil", "detach", "-quiet", "-force", str(self._mountpoint)])


def _null_terminated(passphrase: str) -> bytes:
    # `-stdinpass` reads up to a NUL, so a newline would become part of the passphrase.
    return passphrase.encode() + b"\0"
