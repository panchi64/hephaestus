"""The encrypted volume: the open/close protocol, and each real backend where it runs."""

from __future__ import annotations

import shutil
import sys
import uuid
from pathlib import Path

import pytest

from core.volume import (
    UnavailableVolume,
    VolumeError,
    VolumeUnavailable,
    close_volume,
    open_volume,
)
from core.volume.linux import GocryptfsVolume, fusermount_binary
from core.volume.macos import HdiutilVolume

_KEY = bytes(range(32))
_MARKER = b"PLAINTEXT-VOLUME-MARKER"


class _RecordingVolume:
    def __init__(self, mountpoint: Path, *, mounted: bool = False) -> None:
        self._mountpoint: Path = mountpoint
        self.mounted: bool = mounted
        self.created: bool = False
        self.calls: list[str] = []

    @property
    def mountpoint(self) -> Path:
        return self._mountpoint

    def exists(self) -> bool:
        return self.created

    def is_mounted(self) -> bool:
        return self.mounted

    async def create(self, passphrase: str) -> None:
        self.calls.append(f"create:{passphrase}")
        self.created = True

    async def mount(self, passphrase: str) -> None:
        self.calls.append(f"mount:{passphrase}")
        self.mounted = True

    async def unmount(self) -> None:
        self.calls.append("unmount")
        self.mounted = False


async def test_first_open_creates_then_mounts_owner_only(tmp_path: Path):
    volume = _RecordingVolume(tmp_path / "volume")
    await open_volume(volume, _KEY)
    assert volume.calls == [f"create:{_KEY.hex()}", f"mount:{_KEY.hex()}"]
    assert volume.mountpoint.stat().st_mode & 0o077 == 0


async def test_a_stale_mount_is_unmounted_before_the_key_is_proven_again(tmp_path: Path):
    volume = _RecordingVolume(tmp_path / "volume", mounted=True)
    volume.created = True
    await open_volume(volume, _KEY)
    assert volume.calls == ["unmount", f"mount:{_KEY.hex()}"]

    await close_volume(volume)
    await close_volume(volume)
    assert volume.calls[-1] == "unmount" and volume.calls.count("unmount") == 2


async def test_an_unavailable_backend_refuses_with_its_reason(tmp_path: Path):
    volume = UnavailableVolume(tmp_path / "volume", "install gocryptfs")
    with pytest.raises(VolumeUnavailable, match="install gocryptfs"):
        await open_volume(volume, _KEY)


def _short_dir() -> Path:
    # Under /tmp rather than pytest's deep tmp_path: mount paths show in `mount` output,
    # and a fixed short prefix keeps them easy to find and clean.
    path = Path("/tmp") / f"heph-vol-{uuid.uuid4().hex[:8]}"
    path.mkdir(mode=0o700)
    return path.resolve()


@pytest.mark.skipif(sys.platform != "darwin", reason="hdiutil is macOS-only")
async def test_hdiutil_volume_round_trip_leaves_only_ciphertext():
    root = _short_dir()
    image = root / "volume.sparsebundle"
    volume = HdiutilVolume(image, root / "mnt", max_size="64m")
    try:
        await open_volume(volume, _KEY)
        _ = (volume.mountpoint / "note.txt").write_bytes(_MARKER)
        await close_volume(volume)
        assert not volume.is_mounted()
        for band in image.rglob("*"):
            if band.is_file():
                assert _MARKER not in band.read_bytes()

        with pytest.raises(VolumeError):
            await open_volume(volume, bytes(32))
        await open_volume(volume, _KEY)
        assert (volume.mountpoint / "note.txt").read_bytes() == _MARKER
    finally:
        await close_volume(volume)
        shutil.rmtree(root)


@pytest.mark.skipif(
    shutil.which("gocryptfs") is None or fusermount_binary() is None,
    reason="gocryptfs and fuse3 are not installed",
)
async def test_gocryptfs_volume_round_trip_leaves_only_ciphertext():
    root = _short_dir()
    fusermount = fusermount_binary()
    assert fusermount is not None
    volume = GocryptfsVolume(root / "volume.gocryptfs", root / "mnt", fusermount=fusermount)
    try:
        await open_volume(volume, _KEY)
        _ = (volume.mountpoint / "note.txt").write_bytes(_MARKER)
        await close_volume(volume)
        for file in (root / "volume.gocryptfs").rglob("*"):
            if file.is_file():
                assert _MARKER not in file.read_bytes()
        with pytest.raises(VolumeError):
            await open_volume(volume, bytes(32))
        await open_volume(volume, _KEY)
        assert (volume.mountpoint / "note.txt").read_bytes() == _MARKER
    finally:
        await close_volume(volume)
        shutil.rmtree(root)
