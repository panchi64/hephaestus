"""The `/views` REST surface: today just the live-preview status check
(`GET /views/live/status`) — lets a client tell a still-running `view.live` head
apart from one whose machine was idle-reaped (or purged) without an explicit
`view.live.stopped` signal to catch."""

from __future__ import annotations

from pathlib import Path
from typing import final, override

from routes.deps import app_state
from services.vm import ConversationMachines, GuestImage, Platform, Previews

from ._helpers import client_app


@final
class _FakePreviews(Previews):
    def __init__(self, status: str) -> None:
        machines = ConversationMachines(
            Platform(GuestImage(Path("/nonexistent")), enabled=False),
            prefix="heph-test",
            cpus=1,
            memory_mb=256,
            idle_ttl_s=60.0,
            reap_interval_s=60.0,
            max_machines=1,
        )
        super().__init__(machines, startup_timeout_s=1.0)
        self._answer: str = status
        self.checked: list[str] = []

    @override
    def status(self, token: str) -> str:
        self.checked.append(token)
        return self._answer


async def test_live_status_reports_running():
    async with client_app() as (client, app):
        previews = _FakePreviews("running")
        app_state(app).previews = previews
        resp = await client.get("/views/live/status", params={"token": "tok-1"})
        assert resp.status_code == 200
        assert resp.json() == {"status": "running"}
        assert previews.checked == ["tok-1"]


async def test_live_status_reports_stopped_after_a_reap():
    async with client_app() as (client, app):
        app_state(app).previews = _FakePreviews("stopped")
        resp = await client.get("/views/live/status", params={"token": "tok-1"})
        assert resp.json() == {"status": "stopped"}


async def test_live_status_is_unknown_with_no_previews():
    async with client_app() as (client, app):
        app_state(app).previews = None
        resp = await client.get("/views/live/status", params={"token": "tok-1"})
        assert resp.status_code == 200
        assert resp.json() == {"status": "unknown"}
