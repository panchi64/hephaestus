"""The `/views` REST surface: today just the live-preview status check
(`GET /views/live/status`) — lets a client tell a still-running `view.live` head
apart from one the sandbox idle-reaped (or purged) without an explicit
`view.live.stopped` signal to catch."""

from __future__ import annotations

from ._helpers import client_app


class _FakeSessions:
    def __init__(self, status: str) -> None:
        self._status: str = status
        self.checked: list[str] = []

    def preview_status(self, token: str) -> str:
        self.checked.append(token)
        return self._status


async def test_live_status_reports_running():
    async with client_app() as (client, app):
        app.state.sandbox = _FakeSessions("running")
        resp = await client.get("/views/live/status", params={"token": "tok-1"})
        assert resp.status_code == 200
        assert resp.json() == {"status": "running"}
        assert app.state.sandbox.checked == ["tok-1"]


async def test_live_status_reports_stopped_after_a_reap():
    async with client_app() as (client, app):
        app.state.sandbox = _FakeSessions("stopped")
        resp = await client.get("/views/live/status", params={"token": "tok-1"})
        assert resp.json() == {"status": "stopped"}


async def test_live_status_is_unknown_with_no_sandbox_manager():
    async with client_app() as (client, app):
        app.state.sandbox = None
        resp = await client.get("/views/live/status", params={"token": "tok-1"})
        assert resp.status_code == 200
        assert resp.json() == {"status": "unknown"}
