"""A workspace whose database was deleted: detecting it, and starting fresh.

The trap this covers: what says a workspace exists is `data/keyfile.json`, not
`data/app.db`. An operator who clears the database to start over is otherwise asked
to unlock a key that now protects nothing, and `/setup` answers 409.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from fastapi import FastAPI

from app import create_app
from core.config import Settings
from routes.deps import app_state
from services.workspace_reset import reset_workspace

from ._helpers import client_app
from ._json import arr, json_body


def _settings(data_dir: Path) -> Settings:
    return Settings(
        db_url=None,  # a real file under data_dir — the whole point of these tests
        data_dir=data_dir,
        worktrees_dir=data_dir.parent / "worktrees",
        auth_enabled=True,
        unlock_passphrase=None,
        searxng_enabled=False,
        web_fetch_enabled=False,
        sandbox_enabled=False,
        offline_check_enabled=False,
    )


# ── Detection ──────────────────────────────────────────────────────────────────


def _db_intact(app: FastAPI) -> bool:
    return app_state(app).workspace_db_intact


async def test_a_missing_database_is_noticed_at_boot(tmp_path: Path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    # A genuinely fresh install creates the database at boot, so the flag is false
    # here too — harmless, because there is no keyfile for it to disagree with, and
    # `/setup` marks the two in step the moment a workspace exists.
    app = create_app(_settings(data_dir))
    async with app.router.lifespan_context(app):
        assert _db_intact(app) is False
    assert (data_dir / "app.db").exists()

    # Second boot, database still there.
    app = create_app(_settings(data_dir))
    async with app.router.lifespan_context(app):
        assert _db_intact(app) is True

    # Delete it the way an operator would, then boot again.
    (data_dir / "app.db").unlink()
    app = create_app(_settings(data_dir))
    async with app.router.lifespan_context(app):
        assert _db_intact(app) is False


async def test_db_missing_is_reported_only_when_a_key_outlived_the_database():
    async with client_app(auth_enabled=True, passphrase=None) as (client, app):
        # No keyfile yet: a fresh install is first-run, never a broken workspace.
        app.state.workspace_db_intact = False
        assert json_body(await client.get("/auth/status"))["db_missing"] is False

        _ = await client.post("/setup", json={"password": "correct horse"})
        # Setup put the workspace in *this* database, so the two are in step again.
        assert json_body(await client.get("/auth/status"))["db_missing"] is False

        app.state.workspace_db_intact = False
        assert json_body(await client.get("/auth/status"))["db_missing"] is True


# ── The reset endpoint's guards ────────────────────────────────────────────────


async def test_reset_refuses_when_there_is_nothing_set_up():
    async with client_app(auth_enabled=True, passphrase=None) as (client, app):
        app.state.workspace_db_intact = False
        assert (await client.post("/setup/reset")).status_code == 409


async def test_reset_refuses_while_the_workspace_database_is_intact():
    async with client_app(auth_enabled=True, passphrase=None) as (client, _app):
        _ = await client.post("/setup", json={"password": "correct horse"})
        _ = await client.post("/auth/lock")
        # The database predates this boot — a live workspace, which this endpoint
        # must never be able to reach.
        assert (await client.post("/setup/reset")).status_code == 409


async def test_reset_refuses_while_the_vault_is_unlocked():
    async with client_app(auth_enabled=True, passphrase=None) as (client, app):
        _ = await client.post("/setup", json={"password": "correct horse"})
        app.state.workspace_db_intact = False
        # Whoever is asking is already inside, and has ordinary ways to delete data.
        assert (await client.post("/setup/reset")).status_code == 409


async def test_reset_removes_the_key_and_lands_on_first_run():
    async with client_app(auth_enabled=True, passphrase=None) as (client, app):
        _ = await client.post("/setup", json={"password": "correct horse"})
        keyfile = app_state(app).settings.data_dir / "keyfile.json"
        assert keyfile.exists()

        _ = await client.post("/auth/lock")
        app.state.workspace_db_intact = False

        res = await client.post("/setup/reset")
        assert res.status_code == 200
        assert "keyfile.json" in arr(json_body(res)["removed"])
        assert json_body(res)["failed"] == []

        # No restart needed: the vault re-stats the file on every read.
        assert not keyfile.exists()
        status = json_body(await client.get("/auth/status"))
        assert status["initialized"] is False
        assert status["db_missing"] is False

        # And setup is genuinely offered again.
        assert (await client.post("/setup", json={"password": "a new one"})).status_code == 200


# ── The wipe itself ────────────────────────────────────────────────────────────


def test_the_wipe_spares_the_live_database_and_searxng_only(tmp_path: Path):
    _ = (tmp_path / "keyfile.json").write_text("{}")
    _ = (tmp_path / "app.db").write_text("x" * 10)
    _ = (tmp_path / "app.db-wal").write_text("x" * 5)
    (tmp_path / "searxng").mkdir()
    _ = (tmp_path / "searxng" / "secret_key").write_text("keep me")
    (tmp_path / "uploads").mkdir()
    _ = (tmp_path / "uploads" / "sealed.bin").write_text("y" * 20)
    # A sealed directory nobody has thought of yet must go by default, not survive.
    (tmp_path / "some-future-sealed-store").mkdir()
    _ = (tmp_path / "some-future-sealed-store" / "blob").write_text("z" * 30)

    summary = reset_workspace(tmp_path)

    assert (tmp_path / "app.db").exists()
    assert (tmp_path / "app.db-wal").exists()
    assert (tmp_path / "searxng" / "secret_key").exists()
    assert not (tmp_path / "keyfile.json").exists()
    assert not (tmp_path / "uploads").exists()
    assert not (tmp_path / "some-future-sealed-store").exists()

    assert set(summary.removed) == {"keyfile.json", "uploads", "some-future-sealed-store"}
    assert summary.bytes_freed == 2 + 20 + 30
    assert summary.failed == []


def test_the_wipe_is_a_no_op_on_a_directory_that_is_not_there():
    with tempfile.TemporaryDirectory() as tmp:
        summary = reset_workspace(Path(tmp) / "gone")
    assert summary.removed == []
    assert summary.failed == []
