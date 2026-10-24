"""Each conversation's scratch folder on the host: ``~/scratch`` to the agent."""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import Collection
from pathlib import Path

from services.sandbox.base import SandboxError, contained_path, safe_key
from services.sandbox.walk import walk_files


class HostFiles:
    """Files in a host folder, as staging reads and writes them. A staged filename is
    operator content, so every path is held inside the folder."""

    def __init__(self, root: Path) -> None:
        self._root: Path = root

    def ensure_workspace(self) -> Path:
        self._root.mkdir(parents=True, exist_ok=True)
        return self._root

    def read_file(self, relpath: str) -> bytes:
        target = contained_path(self._root, relpath)
        if not target.is_file():
            raise SandboxError(f"no such file in the workspace: {relpath!r}")
        return target.read_bytes()

    def write_file(self, relpath: str, content: bytes) -> None:
        target = contained_path(self.ensure_workspace(), relpath)
        target.parent.mkdir(parents=True, exist_ok=True)
        _ = target.write_bytes(content)

    def collect_text_files(
        self, excludes: Collection[str], *, max_file_bytes: int = 262_144, max_files: int = 2000
    ) -> dict[str, bytes]:
        """The folder's UTF-8 text files (relpath → bytes) for a history snapshot, minus
        those over ``max_file_bytes``. Synchronous file IO; call off the event loop."""
        if not self._root.exists():
            return {}
        files: dict[str, bytes] = {}
        for rel, full in walk_files(self._root, excludes):
            if len(files) >= max_files:
                return files
            try:
                if full.stat().st_size > max_file_bytes:
                    continue
                data = full.read_bytes()
            except OSError:
                continue
            if b"\x00" in data:
                continue  # a NUL is valid UTF-8, so the decode below wouldn't catch it
            try:
                _ = data.decode("utf-8")
            except UnicodeDecodeError:
                continue
            files[rel] = data
        return files


class ScratchDirs:
    """Every conversation's scratch, under ``<data>/sandbox/work``."""

    def __init__(self, root: Path, *, excludes: tuple[str, ...] = ()) -> None:
        self._root: Path = root
        self._excludes: tuple[str, ...] = excludes

    @property
    def root(self) -> Path:
        return self._root

    @property
    def walk_excludes(self) -> tuple[str, ...]:
        """What a walk of a scratch skips: virtualenvs, caches, ``.git``."""
        return self._excludes

    def path(self, key: str) -> Path:
        return self._root / safe_key(key)

    def acquire(self, key: str) -> HostFiles:
        return HostFiles(self.path(key))

    def settled_workspace(self, key: str) -> Path | None:
        """This key's scratch if it is already on disk; creates nothing."""
        path = self.path(key)
        return path if path.is_dir() else None

    async def purge(self, key: str) -> None:
        await asyncio.to_thread(shutil.rmtree, self.path(key), True)
