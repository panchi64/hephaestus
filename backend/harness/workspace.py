"""The open workspace: the decrypted view of the operator's data, alive only while unlocked.

Opening mounts the encrypted volume, builds the SQLCipher engine under the vault's ``db``
subkey, migrates it, and wires every data-bearing unit (`harness.workspace_wiring`).
Locking reverses all of it — runs cancelled and flushed, units stopped, the engine
disposed, the volume unmounted — and only then zeroes the key. A locked process holds no
open handle onto plaintext.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
from enum import StrEnum
from pathlib import Path

import httpx
from fastapi import FastAPI

import core.db
from core.config import Settings
from core.exceptions import HephaestusError
from core.shutdown import begin_shutdown, reset_shutdown
from core.vault import Vault
from core.volume import EncryptedVolume, close_volume, open_volume
from harness import LifecycleRegistry
from harness.workspace_wiring import wire_workspace
from routes.deps import app_state
from services.vm import Platform

logger = logging.getLogger(__name__)


class WorkspaceOpenError(HephaestusError):
    """The workspace did not open; the message is the reason shown on the unlock screen."""


class WorkspaceStatus(StrEnum):
    CLOSED = "closed"
    OPENING = "opening"
    OPEN = "open"
    FAILED = "failed"


class WorkspaceSession:
    def __init__(
        self,
        app: FastAPI,
        settings: Settings,
        *,
        vault: Vault,
        volume: EncryptedVolume,
        platform: Platform,
        discovery_client: httpx.AsyncClient,
    ) -> None:
        self._app: FastAPI = app
        self._settings: Settings = settings
        self._vault: Vault = vault
        self._volume: EncryptedVolume = volume
        self._platform: Platform = platform
        self._discovery_client: httpx.AsyncClient = discovery_client
        self._lifecycle: LifecycleRegistry | None = None
        self._guard: asyncio.Lock = asyncio.Lock()
        self.status: WorkspaceStatus = WorkspaceStatus.CLOSED
        #: Why the last open failed, for the unlock screen; cleared by a successful open.
        self.failure: str | None = None

    @property
    def is_open(self) -> bool:
        return self.status is WorkspaceStatus.OPEN

    def database_url(self) -> str:
        return self._settings.db_url or f"sqlite:///{self._settings.data_dir / 'app.db'}"

    async def open(self) -> None:
        """Mount, decrypt and wire the workspace. Requires an unlocked vault.

        A failure unwinds whatever came up and leaves the workspace closed, with the
        reason in :attr:`failure`; the caller re-locks the vault.
        """
        async with self._guard:
            if self.is_open:
                return
            self.status = WorkspaceStatus.OPENING
            lifecycle = LifecycleRegistry()
            try:
                await self._open_into(lifecycle)
            except BaseException as exc:
                await lifecycle.stop_all()
                self.status = WorkspaceStatus.FAILED
                self.failure = str(exc) or type(exc).__name__
                logger.exception("workspace: opening failed")
                if isinstance(exc, Exception):
                    raise WorkspaceOpenError(self.failure) from exc
                raise
            self._lifecycle = lifecycle
            self.status = WorkspaceStatus.OPEN
            self.failure = None

    async def _open_into(self, lifecycle: LifecycleRegistry) -> None:
        settings = self._settings
        await open_volume(self._volume, self._vault.subkey("volume"))
        # First registered, so it stops last: everything below reads or writes the volume.
        lifecycle.on_stop("volume", lambda: close_volume(self._volume))
        _use_volume_tmp(settings.volume_tmp_dir)
        lifecycle.on_stop("volume-tmp", _restore_tmp)

        url = self.database_url()
        in_memory = ":memory:" in url
        engine = core.db.make_engine(url, key=None if in_memory else self._vault.subkey("db"))
        lifecycle.on_stop("db-engine", engine.dispose)
        # Read through the module so the suite's cached-schema `init_db` applies.
        await asyncio.to_thread(core.db.init_db, engine)
        await wire_workspace(
            self._app,
            settings,
            lifecycle,
            engine=engine,
            vault=self._vault,
            platform=self._platform,
            discovery_client=self._discovery_client,
        )

    async def close(self) -> None:
        """Stop every workspace unit, dispose the engine and unmount. Idempotent."""
        async with self._guard:
            lifecycle, self._lifecycle = self._lifecycle, None
            if lifecycle is not None:
                await lifecycle.stop_all()
            if self.status is not WorkspaceStatus.FAILED:
                self.status = WorkspaceStatus.CLOSED
            app_state(self._app).api_tokens = None

    async def lock(self) -> None:
        """Close the workspace, then zero the key; streams end as at process shutdown."""
        # Long-lived responses (notification feed, a parked run's stream) wait on this
        # flag; without it they would hold onto stores that are about to stop.
        begin_shutdown()
        try:
            await self.close()
        finally:
            reset_shutdown()
        self._vault.lock()
        # The callbacks belonged to this session's units; the next open registers fresh ones.
        self._vault.clear_lock_callbacks()


def _use_volume_tmp(tmp_dir: Path) -> None:
    """Point this process's temp files into the volume, emptied of a previous session's."""
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir)
    tmp_dir.mkdir(mode=0o700, parents=True)
    tempfile.tempdir = str(tmp_dir)


def _restore_tmp() -> None:
    tempfile.tempdir = None
