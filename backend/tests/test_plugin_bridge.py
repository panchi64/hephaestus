"""The plugin window bridge: permissions, storage, the operator's confirmation, and grants."""

from __future__ import annotations

import io
import zipfile

import httpx
import pytest
from fastapi import FastAPI

from routes import deps, plugin_bridge
from routes.chat.schemas import ChatCreate, ChatCreated
from services.plugins.bridge.base import Confirmation
from services.plugins.bridge.gate import PENDING_TTL_S, BridgeGate
from services.plugins.storage import VALUE_MAX_BYTES
from services.plugins.windows import WindowSessions

from ._helpers import client_app
from ._json import json_body, s

PAGE = b"<!doctype html><p>hi</p>"


def _manifest(name: str, permissions: str) -> str:
    return (
        f"name: {name}\ndescription: A window.\n"
        + "windows:\n  - id: main\n    title: Board\n    entry: ui/index.html\n"
        + "    size: {width: 300, height: 300}\n"
        + f"permissions: [{permissions}]\n"
    )


def _zip(name: str, manifest: str, page: bytes = PAGE) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(f"{name}/plugin.yaml", manifest)
        archive.writestr(f"{name}/ui/index.html", page)
    return buffer.getvalue()


async def _open(client: httpx.AsyncClient, name: str, permissions: str) -> tuple[str, str]:
    """Publish a window plugin and open its window; returns its id and bridge token."""
    archive = _zip(name, _manifest(name, permissions))
    imported = await client.post(
        "/plugins/import", files={"file": (f"{name}.zip", archive, "application/zip")}
    )
    plugin_id = s(json_body(imported)["id"])
    _ = await client.post(f"/plugins/{plugin_id}/publish")
    opened = json_body(await client.post(f"/plugins/{plugin_id}/windows/main/sessions"))
    return plugin_id, s(opened["bridge_token"])


async def _bridge(
    client: httpx.AsyncClient, token: str, method: str, params: dict[str, object] | None = None
) -> httpx.Response:
    return await client.post(
        f"/plugins/bridge/{method}", json=params or {}, headers={"X-Plugin-Window": token}
    )


async def test_a_window_keeps_its_own_values_and_cannot_see_another_plugins():
    async with client_app() as (client, _app):
        _id, board = await _open(client, "board", "storage")
        _id, other = await _open(client, "other", "storage")

        saved = json_body(
            await _bridge(client, board, "storage.set", {"key": "best", "value": {"n": 42}})
        )
        assert (saved["outcome"], saved["result"]) == ("done", None)
        got = json_body(await _bridge(client, board, "storage.get", {"key": "best"}))
        assert got["result"] == {"n": 42}
        assert json_body(await _bridge(client, board, "storage.list"))["result"] == ["best"]

        theirs = json_body(await _bridge(client, other, "storage.get", {"key": "best"}))
        assert theirs["result"] is None

        _ = await _bridge(client, board, "storage.delete", {"key": "best"})
        assert json_body(await _bridge(client, board, "storage.list"))["result"] == []


async def test_a_value_over_the_quota_is_refused():
    async with client_app() as (client, _app):
        _id, board = await _open(client, "board", "storage")
        huge = "x" * (VALUE_MAX_BYTES + 1)
        refused = await _bridge(client, board, "storage.set", {"key": "big", "value": huge})
        assert refused.status_code == 422


async def test_a_method_the_plugin_did_not_declare_is_forbidden():
    async with client_app() as (client, _app):
        _id, board = await _open(client, "board", "storage")
        refused = await _bridge(client, board, "chat.post", {"prompt": "hi"})
        assert refused.status_code == 403
        unknown = await _bridge(client, board, "files.read", {})
        assert unknown.status_code == 404


async def test_a_closed_window_cannot_call():
    async with client_app() as (client, _app):
        plugin_id, board = await _open(client, "board", "storage")
        _ = await client.post(f"/plugins/{plugin_id}/unpublish")
        assert (await _bridge(client, board, "storage.list")).status_code == 404
        assert (await _bridge(client, "made-up", "storage.list")).status_code == 404


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[ChatCreate]:
    """Every chat a window posted, without starting a run."""
    posted: list[ChatCreate] = []

    async def fake_send(app: FastAPI, body: ChatCreate) -> ChatCreated:
        del app
        posted.append(body)
        return ChatCreated(run_id="run-1", conversation_id=body.conversation_id or "conv-new")

    monkeypatch.setattr(plugin_bridge, "send_operator_message", fake_send)
    return posted


async def test_posting_to_chat_waits_for_the_operator_and_always_allow_sticks(
    sent: list[ChatCreate],
):
    async with client_app() as (client, _app):
        _id, board = await _open(client, "board", "chat.post")

        asked = json_body(await _bridge(client, board, "chat.post", {"prompt": "I won!"}))
        assert asked["outcome"] == "confirm"
        assert "I won!" in s(asked["summary"])
        assert sent == []

        confirmation = f"/plugins/bridge/confirmations/{s(asked['confirmation_id'])}"
        headers = {"X-Plugin-Window": board}
        done = json_body(await client.post(confirmation, json={"always": True}, headers=headers))
        assert (done["outcome"], done["result"]) == ("done", {"conversation_id": "conv-new"})
        assert [body.prompt for body in sent] == ["I won!"]

        again = await client.post(confirmation, json={"always": False}, headers=headers)
        assert again.status_code == 404

        direct = json_body(await _bridge(client, board, "chat.post", {"prompt": "Again"}))
        assert direct["outcome"] == "done"
        assert [body.prompt for body in sent] == ["I won!", "Again"]


async def test_a_new_version_of_the_plugin_asks_again(sent: list[ChatCreate]):
    async with client_app() as (client, _app):
        plugin_id, board = await _open(client, "board", "chat.post")
        asked = json_body(await _bridge(client, board, "chat.post", {"prompt": "hi"}))
        _ = await client.post(
            f"/plugins/bridge/confirmations/{s(asked['confirmation_id'])}",
            json={"always": True},
            headers={"X-Plugin-Window": board},
        )

        archive = _zip("board", _manifest("board", "chat.post"), page=b"<p>new code</p>")
        _ = await client.delete(f"/plugins/{plugin_id}")
        imported = await client.post(
            "/plugins/import", files={"file": ("board.zip", archive, "application/zip")}
        )
        new_id = s(json_body(imported)["id"])
        _ = await client.post(f"/plugins/{new_id}/publish")
        opened = json_body(await client.post(f"/plugins/{new_id}/windows/main/sessions"))
        rewritten = s(opened["bridge_token"])

        asked_again = json_body(await _bridge(client, rewritten, "chat.post", {"prompt": "hi"}))
        assert asked_again["outcome"] == "confirm"
        assert len(sent) == 1


async def test_a_parked_call_expires():
    async with client_app() as (client, app):
        _id, board = await _open(client, "board", "chat.post")
        clock = [0.0]
        gate = BridgeGate(deps.settings_store(app), now=lambda: clock[0])
        window = deps.service(app, WindowSessions).bridge(board)
        assert window is not None
        confirmation_id = gate.ask(window, "chat.post", {"prompt": "hi"}, Confirmation("x", "y"))
        clock[0] = PENDING_TTL_S + 1
        assert gate.take(window, confirmation_id) is None
