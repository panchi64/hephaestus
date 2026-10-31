"""Linux backend: a gocryptfs (AES-256-GCM) directory mounted through FUSE, no root needed."""

from __future__ import annotations

import shutil
from pathlib import Path

from core.volume.base import is_mountpoint, run_tool


class GocryptfsVolume:
    def __init__(self, cipherdir: Path, mountpoint: Path, *, fusermount: str) -> None:
        self._cipherdir: Path = cipherdir
        self._mountpoint: Path = mountpoint
        self._fusermount: str = fusermount

    @property
    def mountpoint(self) -> Path:
        return self._mountpoint

    def exists(self) -> bool:
        return (self._cipherdir / "gocryptfs.conf").exists()

    def is_mounted(self) -> bool:
        return is_mountpoint(self._mountpoint)

    async def create(self, passphrase: str) -> None:
        self._cipherdir.mkdir(mode=0o700, parents=True, exist_ok=True)
        # gocryptfs reads the password from stdin when stdin is not a terminal.
        await run_tool(["gocryptfs", "-init", "-q", str(self._cipherdir)], stdin=_line(passphrase))

    async def mount(self, passphrase: str) -> None:
        await run_tool(
            ["gocryptfs", "-q", "-noprealloc", str(self._cipherdir), str(self._mountpoint)],
            stdin=_line(passphrase),
        )

    async def unmount(self) -> None:
        await run_tool([self._fusermount, "-u", str(self._mountpoint)])


def fusermount_binary() -> str | None:
    return shutil.which("fusermount3") or shutil.which("fusermount")


def _line(passphrase: str) -> bytes:
    return passphrase.encode() + b"\n"
