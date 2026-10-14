"""The app's side of the agent's browser: its session, the live view, and the profile.

A fake session manager stands in for the real one — what is worth guarding here is the
routes' contract (a thread with no browser is not an error, a browser that cannot be opened
says so rather than reporting a success, and the view's input reaches CDP as CDP expects
it), none of which needs a browser.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from fastapi import FastAPI
from starlette.testclient import TestClient

from app import create_app
from core.config import Settings


class _FakeCdp:
    """A CDP session that records what it was sent, and answers a screencast start with
    one frame."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, object]]] = []
        self._handlers: dict[str, Callable[[dict[str, object]], object]] = {}
        self.on_ack = lambda: None

    def on(self, event: str, handler: Callable[[dict[str, object]], object]) -> None:
        self._handlers[event] = handler

    async def send(self, method: str, params: dict[str, object] | None = None) -> dict[str, object]:
        self.sent.append((method, params or {}))
        if method == "Page.screencastFrameAck":
            self.on_ack()
        if method == "Page.startScreencast":
            self._handlers["Page.screencastFrame"](
                {
                    "data": "AAAA",
                    "sessionId": 7,
                    "metadata": {"deviceWidth": 1280, "deviceHeight": 720},
                }
            )
        return {}

    async def detach(self) -> None:
        pass


class _FakeContext:
    def __init__(self) -> None:
        self.cdp = _FakeCdp()

    async def new_cdp_session(self, page) -> _FakeCdp:
        return self.cdp


class _FakePage:
    def __init__(self) -> None:
        self.url = "https://example.com/app"
        self.context = _FakeContext()
        self.visited: list[str] = []

    async def title(self) -> str:
        return "App"

    async def goto(self, url: str, **_kwargs) -> None:
        self.visited.append(url)


class _FakeSession:
    def __init__(self) -> None:
        self.page = _FakePage()


class _FakeLive:
    def __init__(self, token: str = "tok") -> None:
        self.token = token
        self.page_url = "https://example.com/app"
        self.session = _FakeSession()
        self.input_channel = None
        self.touched = 0

    def touch(self) -> None:
        self.touched += 1


class _FakeManager:
    """Holds one conversation's browser, and records what was asked of it."""

    def __init__(self, live: _FakeLive | None, *, key: str = "c1") -> None:
        self._live = live
        self._key = key
        self.acquired: list[str] = []
        self.ended: list[str] = []
        self.forgot = 0

    def existing(self, key: str) -> _FakeLive | None:
        return self._live if key == self._key else None

    async def acquire(self, key: str) -> _FakeLive | None:
        self.acquired.append(key)
        return self._live

    async def close(self, key: str) -> None:
        self.ended.append(key)
        self._live = None

    async def forget_profile(self) -> None:
        self.forgot += 1


def _app[M: _FakeManager | None](tmp_path: Path, manager: M) -> tuple[FastAPI, TestClient, M]:
    settings = Settings(
        db_url="sqlite:///:memory:",
        data_dir=tmp_path,
        auth_enabled=False,
        unlock_passphrase="pw",
    )
    app = create_app(settings)
    client = TestClient(app)
    return app, client, manager


# --- session lookup --------------------------------------------------------------------


def test_a_conversation_reports_its_live_browser(tmp_path):
    # What a freshly-loaded page reads: the run events that touched the browser are long
    # gone by then, and the manager is the source of truth for what is live right now.
    app, client, manager = _app(tmp_path, _FakeManager(_FakeLive()))
    with client:
        app.state.browser_sessions = manager
        assert client.get("/browser/session/c1").json() == {
            "active": True,
            "page_url": "https://example.com/app",
        }
        assert client.get("/browser/session/other").json()["active"] is False
    assert manager.acquired == []  # asking never opens one


def test_no_browser_control_wired_is_not_an_error_for_a_conversation(tmp_path):
    # A thread simply has no browser; the UI must not have to special-case a 500.
    app, client, _ = _app(tmp_path, None)
    with client:
        app.state.browser_sessions = None
        assert client.get("/browser/session/c1").json()["active"] is False


# --- opening one -----------------------------------------------------------------------


def test_opening_a_browser_attaches_it(tmp_path):
    app, client, manager = _app(tmp_path, _FakeManager(_FakeLive()))
    with client:
        app.state.browser_sessions = manager
        body = client.post("/browser/session/c1")
        assert body.status_code == 200
        assert body.json() == {"active": True, "page_url": "https://example.com/app"}
    assert manager.acquired == ["c1"]


def test_a_browser_that_cannot_be_opened_is_a_503_with_a_reason(tmp_path):
    app, client, manager = _app(tmp_path, _FakeManager(None))
    with client:
        app.state.browser_sessions = manager
        response = client.post("/browser/session/c1")
        assert response.status_code == 503
        assert response.json()["detail"]


def test_opening_one_with_no_browser_control_wired_is_a_503(tmp_path):
    # Unlike the lookup, a request to *open* a window must never report success quietly.
    app, client, _ = _app(tmp_path, None)
    with client:
        app.state.browser_sessions = None
        assert client.post("/browser/session/c1").status_code == 503


# --- ending it, and the profile ----------------------------------------------------------


def test_the_operator_can_end_a_conversations_browser(tmp_path):
    app, client, manager = _app(tmp_path, _FakeManager(_FakeLive()))
    with client:
        app.state.browser_sessions = manager
        assert client.delete("/browser/session/c1").status_code == 204
    assert manager.ended == ["c1"]


def test_forgetting_the_profile_reaches_the_manager(tmp_path):
    app, client, manager = _app(tmp_path, _FakeManager(None))
    with client:
        app.state.browser_sessions = manager
        assert client.delete("/browser/profile").status_code == 204
    assert manager.forgot == 1


# --- the live view -----------------------------------------------------------------------


def test_input_without_a_browser_is_a_404(tmp_path):
    app, client, manager = _app(tmp_path, _FakeManager(None))
    with client:
        app.state.browser_sessions = manager
        response = client.post("/browser/session/c1/input", json={"type": "text", "text": "hello"})
        assert response.status_code == 404


def test_a_click_reaches_cdp_as_a_mouse_event(tmp_path):
    live = _FakeLive()
    app, client, manager = _app(tmp_path, _FakeManager(live))
    with client:
        app.state.browser_sessions = manager
        response = client.post(
            "/browser/session/c1/input",
            json={
                "type": "mouse",
                "action": "down",
                "x": 10,
                "y": 20,
                "button": "left",
                "click_count": 1,
                "modifiers": 8,
            },
        )
        assert response.status_code == 204
    method, params = live.session.page.context.cdp.sent[-1]
    assert method == "Input.dispatchMouseEvent"
    assert params == {
        "type": "mousePressed",
        "x": 10,
        "y": 20,
        "button": "left",
        "clickCount": 1,
        "modifiers": 8,
    }
    assert live.touched == 1  # the operator's input counts as use


def test_a_typed_character_carries_its_text(tmp_path):
    live = _FakeLive()
    app, client, manager = _app(tmp_path, _FakeManager(live))
    with client:
        app.state.browser_sessions = manager
        client.post(
            "/browser/session/c1/input",
            json={
                "type": "key",
                "action": "down",
                "key": "a",
                "code": "KeyA",
                "text": "a",
                "key_code": 65,
            },
        )
        client.post(
            "/browser/session/c1/input",
            json={"type": "key", "action": "down", "key": "Enter", "code": "Enter", "key_code": 13},
        )
    typed, enter = live.session.page.context.cdp.sent[-2:]
    assert typed[1]["type"] == "keyDown" and typed[1]["text"] == "a"
    assert enter[1]["type"] == "rawKeyDown" and "text" not in enter[1]


def test_the_address_bar_takes_only_web_urls(tmp_path):
    live = _FakeLive()
    app, client, manager = _app(tmp_path, _FakeManager(live))
    with client:
        app.state.browser_sessions = manager
        refused = client.post(
            "/browser/session/c1/input", json={"type": "navigate", "url": "file:///etc/passwd"}
        )
        assert refused.status_code == 422
        taken = client.post(
            "/browser/session/c1/input", json={"type": "navigate", "url": "https://example.org"}
        )
        assert taken.status_code == 204
    assert live.session.page.visited == ["https://example.org"]


def test_the_view_without_a_browser_is_a_404(tmp_path):
    app, client, manager = _app(tmp_path, _FakeManager(None))
    with client:
        app.state.browser_sessions = manager
        assert client.get("/browser/session/c1/view").status_code == 404


def test_the_view_streams_frames_and_acknowledges_them(tmp_path):
    live = _FakeLive()
    manager = _FakeManager(live)
    # The session ends once its first frame is acknowledged, which ends the stream.
    live.session.page.context.cdp.on_ack = lambda: setattr(manager, "_live", None)
    app, client, _ = _app(tmp_path, manager)
    with client:
        app.state.browser_sessions = manager
        response = client.get("/browser/session/c1/view")
    assert response.status_code == 200
    events = [
        json.loads(line[len("data: ") :])
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]
    frame, closed = events
    assert closed == {"type": "closed"}
    assert frame == {
        "type": "frame",
        "data": "AAAA",
        "width": 1280,
        "height": 720,
        "url": "https://example.com/app",
        "title": "App",
    }
    sent = [method for method, _ in live.session.page.context.cdp.sent]
    assert sent[0] == "Page.startScreencast"
    assert "Page.screencastFrameAck" in sent
