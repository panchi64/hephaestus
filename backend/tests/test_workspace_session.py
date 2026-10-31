"""The workspace session: locked boots touch no data, lock closes it all, unlock reopens it."""

from __future__ import annotations

from pathlib import Path

import pytest

import core.volume
from core.config import Settings
from core.volume import UnavailableVolume
from routes.deps import OPERATOR_ID, app_state

from ._helpers import client_app
from ._json import json_body, s
from ._volume import DirectoryVolume


async def test_lock_disposes_the_engine_and_unlock_reopens_the_same_data(tmp_path: Path):
    async with client_app(auth_enabled=True, passphrase=None, home=tmp_path) as (client, app):
        assert (await client.post("/setup", json={"password": "correct horse"})).status_code == 200
        state = app_state(app)
        await state.settings_store.set(OPERATOR_ID, "probe", "kept across a lock")
        first_engine = state.db_engine
        assert state.workspace.is_open

        assert (await client.post("/auth/lock")).status_code == 200
        assert not state.workspace.is_open and not state.vault.is_unlocked
        assert state.api_tokens is None
        # Disposed: the pool holds no connection onto the decrypted file.
        assert "Connections in pool: 0" in first_engine.pool.status()

        login = await client.post("/auth/login", json={"password": "correct horse"})
        assert login.status_code == 200
        assert state.workspace.is_open and state.db_engine is not first_engine
        assert await state.settings_store.get(OPERATOR_ID, "probe") == "kept across a lock"


async def test_the_database_file_is_encrypted_under_the_vault(tmp_path: Path):
    async with client_app(home=tmp_path) as (_client, app):
        await app_state(app).settings_store.set(OPERATOR_ID, "probe", "PLAINTEXT-SETTING")
    raw = b"".join(p.read_bytes() for p in tmp_path.glob("app.db*"))
    assert raw and b"PLAINTEXT-SETTING" not in raw and b"CREATE TABLE" not in raw
    assert not (tmp_path / "app.db").read_bytes().startswith(b"SQLite format 3\x00")


async def test_the_volume_mounts_at_open_and_unmounts_at_lock(monkeypatch: pytest.MonkeyPatch):
    volumes: list[DirectoryVolume] = []

    def recording(settings: Settings) -> DirectoryVolume:
        volumes.append(DirectoryVolume(settings.volume_dir))
        return volumes[-1]

    monkeypatch.setattr(core.volume, "select_volume", recording)
    async with client_app(auth_enabled=True, passphrase=None) as (client, app):
        (volume,) = volumes
        assert not volume.is_mounted()
        _ = await client.post("/setup", json={"password": "correct horse"})
        assert volume.is_mounted()
        # The volume's passphrase is the vault's volume subkey, not the password.
        assert volume.passphrases[-1] == app_state(app).vault.subkey("volume").hex()
        _ = await client.post("/auth/lock")
        assert not volume.is_mounted()


async def test_an_unavailable_volume_refuses_unlock_and_stays_locked(
    monkeypatch: pytest.MonkeyPatch,
):
    def unavailable(settings: Settings) -> UnavailableVolume:
        return UnavailableVolume(settings.volume_dir, "install gocryptfs and fuse3")

    monkeypatch.setattr(core.volume, "select_volume", unavailable)
    async with client_app(auth_enabled=True, passphrase=None) as (client, app):
        setup = await client.post("/setup", json={"password": "correct horse"})
        assert setup.status_code == 503
        assert "install gocryptfs" in s(json_body(setup)["detail"])
        status = json_body(await client.get("/auth/status"))
        assert status["unlocked"] is False and status["workspace"] == "failed"
        assert "install gocryptfs" in str(status["workspace_failure"])
        assert not app_state(app).vault.is_unlocked
