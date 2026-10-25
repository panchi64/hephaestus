"""Plugin windows: the manifest's rules, going live without publishing, and serving pages."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path
from typing import cast

import pytest
from pydantic_ai.exceptions import ModelRetry

from core.container import ServiceContainer
from core.exceptions import PluginValidationError
from runs import PluginWindowOpened
from services.plugins import PluginStore, validate_files
from services.plugins.windows import MAX_PER_PLUGIN, WindowSessions
from tools.plugins import plugins_toolset

from ._helpers import client_app
from ._json import arr, json_body, json_list, obj, s
from ._plugin_helpers import OWNER, bundle, make_plugin_stores, make_store
from ._recording import RecordingContext, recording_context

SNAKE = """\
name: snake
description: Snake, to play while the agent works.
windows:
  - id: main
    title: Snake
    entry: ui/index.html
    size: {width: 360, height: 420}
permissions: [storage, chat.read]
"""
PAGE = b'<!doctype html><script src="__bridge.js"></script><canvas></canvas>'
PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 16


def snake(manifest: str = SNAKE, extra: dict[str, bytes] | None = None) -> dict[str, bytes]:
    return {"plugin.yaml": manifest.encode(), "ui/index.html": PAGE, **(extra or {})}


def _zip(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for path, blob in files.items():
            archive.writestr(f"snake/{path}", blob)
    return buffer.getvalue()


# --- the manifest ---------------------------------------------------------------


def test_a_window_plugin_reads_its_windows_and_permissions():
    manifest = validate_files(snake(extra={"ui/sprite.png": PNG}))
    (window,) = manifest.windows
    assert (window.id, window.title, window.width, window.height) == ("main", "Snake", 360, 420)
    assert window.min_width <= window.width
    assert manifest.permissions == {"storage", "chat.read"}
    assert manifest.described()["windows"] == [{"name": "main", "title": "Snake"}]


def test_a_ui_only_plugin_is_inert_and_a_tool_plugin_is_not():
    assert validate_files(snake()).inert
    assert not validate_files(bundle()).inert
    with_egress = SNAKE.replace("permissions:", "egress: [example.com]\npermissions:")
    assert not validate_files(snake(with_egress)).inert


@pytest.mark.parametrize(
    ("files", "needle"),
    [
        (snake(SNAKE.replace("ui/index.html", "index.html")), "under ui/"),
        (snake(SNAKE.replace("ui/index.html", "ui/missing.html")), "there is no"),
        (snake(extra={"ui/run.exe": b"MZ"}), "may load only"),
        (snake(extra={"ui/fake.png": b"<svg/>"}), "is not a png"),
        (snake(extra={"ui/bad.js": b"\xff\xfe"}), "UTF-8"),
        (snake(extra={"ui/__bridge.js": b""}), "reserved"),
        (snake(SNAKE.replace("width: 360", "width: 5")), "pixels"),
        (snake(SNAKE.replace("chat.read", "files.write")), "not a permission"),
        (snake(SNAKE.replace("name: snake", "api: 2\nname: snake")), "api 1"),
    ],
)
def test_a_bad_window_bundle_says_what_to_fix(files: dict[str, bytes], needle: str):
    with pytest.raises(PluginValidationError, match=needle):
        _ = validate_files(files)


# --- going live -----------------------------------------------------------------


async def test_a_ui_only_plugin_goes_live_without_publishing(tmp_path: Path):
    store = await make_store(tmp_path)
    view = await store.write_draft(OWNER, "snake", snake(), source="agent")
    live = await store.publish_if_inert(OWNER, view.id)
    assert live.published and live.draft is None


async def test_code_added_to_a_live_ui_plugin_waits_for_the_operator(tmp_path: Path):
    store: PluginStore = await make_store(tmp_path)
    view = await store.write_draft(OWNER, "snake", snake(), source="agent")
    _ = await store.publish_if_inert(OWNER, view.id)
    with_tools = SNAKE + (
        "tools:\n  - name: score\n    description: The score.\n    entry: game:score\n"
    )
    changes: dict[str, bytes | None] = {
        "plugin.yaml": with_tools.encode(),
        "game.py": b"def score():\n    return 1\n",
    }
    edited = await store.write_draft(OWNER, "snake", changes, source="agent")
    after = await store.publish_if_inert(OWNER, edited.id)
    assert after.draft is not None
    assert after.active is not None and not after.active.tools


# --- the agent's tools ----------------------------------------------------------


async def _call(ctx: RecordingContext, name: str, args: dict[str, object]) -> dict[str, object]:
    toolset = plugins_toolset()
    tools = await toolset.get_tools(ctx)
    return cast(dict[str, object], await toolset.call_tool(name, args, ctx, tools[name]))


async def test_the_agent_writes_a_game_and_opens_its_window(tmp_path: Path):
    store, secrets = await make_plugin_stores(tmp_path)
    caps = ServiceContainer()
    caps.add(store)
    caps.add(secrets)
    ctx = recording_context(caps, "conv-1", owner_id=OWNER)
    files = {path: blob.decode() for path, blob in snake().items()}

    written = await _call(ctx, "write", {"name": "snake", "files": files})
    assert (written["status"], written["windows"]) == ("enabled", ["main"])

    opened = await _call(ctx, "open_window", {"name": "snake"})
    assert opened["ok"] is True
    (event,) = ctx.emitted
    assert isinstance(event, PluginWindowOpened)
    assert (event.conversation_id, event.plugin, event.window) == ("conv-1", "snake", "main")

    with pytest.raises(ModelRetry, match="no window"):
        _ = await _call(ctx, "open_window", {"name": "snake", "window": "settings"})


# --- sessions -------------------------------------------------------------------


def test_opening_too_many_windows_drops_the_oldest():
    sessions = WindowSessions()
    (window,) = validate_files(snake()).windows
    opened = [
        sessions.open(
            owner_id=OWNER,
            plugin_id="p",
            plugin="snake",
            window=window,
            permissions=frozenset(),
            files=snake(),
        )
        for _ in range(MAX_PER_PLUGIN + 1)
    ]
    assert sessions.page(opened[0].page_token) is None
    assert sessions.bridge(opened[-1].bridge_token) is opened[-1]
    assert opened[-1].files == {"index.html": PAGE}


# --- serving --------------------------------------------------------------------


async def test_a_window_page_is_served_sandboxed_and_closes_with_its_plugin():
    async with client_app() as (client, _app):
        imported = await client.post(
            "/plugins/import", files={"file": ("snake.zip", _zip(snake()), "application/zip")}
        )
        plugin_id = s(json_body(imported)["id"])
        _ = await client.post(f"/plugins/{plugin_id}/publish")

        windows = json_list(await client.get("/plugins/windows"))
        assert [obj(w)["title"] for w in windows] == ["Snake"]

        opened = await client.post(f"/plugins/{plugin_id}/windows/main/sessions")
        assert opened.status_code == 201, opened.text
        session = json_body(opened)
        src = s(session["src"])
        assert src.endswith("/index.html")
        assert sorted(s(p) for p in arr(session["permissions"])) == ["chat.read", "storage"]

        page = await client.get(src)
        assert page.status_code == 200
        assert page.content == PAGE
        csp = page.headers["content-security-policy"]
        assert csp.startswith("sandbox allow-scripts;")
        assert "allow-same-origin" not in csp
        assert "connect-src 'none'" in csp
        assert page.headers["x-content-type-options"] == "nosniff"

        sdk = await client.get(src.replace("index.html", "__bridge.js"))
        assert sdk.status_code == 200
        assert b"window.odysseus" in sdk.content
        assert (await client.get(src.replace("index.html", "plugin.yaml"))).status_code == 404

        _ = await client.post(f"/plugins/{plugin_id}/unpublish")
        assert (await client.get(src)).status_code == 404
        assert (await client.post(f"/plugins/{plugin_id}/windows/main/sessions")).status_code == 404
