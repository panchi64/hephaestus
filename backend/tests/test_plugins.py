"""Plugins: the manifest's rules, drafts and publishing, history, and secrets."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from core.exceptions import PluginValidationError
from services.plugins import PluginStore, parse_manifest, validate_files
from services.revisions import restore

from ._helpers import client_app
from ._json import arr, json_body, json_list, obj, s
from ._plugin_helpers import CODE, MANIFEST, OWNER, bundle, make_store


@pytest.fixture
async def store(tmp_path: Path) -> PluginStore:
    return await make_store(tmp_path)


# --- the manifest ---------------------------------------------------------------


def test_a_manifest_reads_its_tools_hosts_and_secrets():
    manifest = validate_files(bundle())
    assert manifest.egress == ("api.example.com",)
    assert dict(manifest.secrets) == {"API_KEY": "api.example.com"}
    tool = manifest.tools[0]
    assert (tool.module, tool.function, tool.sensitivity.value) == ("weather", "forecast", "read")


def test_a_tool_is_an_external_effect_unless_it_says_otherwise():
    manifest = parse_manifest(MANIFEST.replace("    sensitivity: read\n", ""))
    assert manifest.tools[0].sensitivity.value == "external_effect"


@pytest.mark.parametrize(
    ("change", "field"),
    [
        (("name: weather", "name: Weather Now"), "name"),
        (("entry: weather:forecast", "entry: missing:forecast"), "tools"),
        (("entry: weather:forecast", "entry: weather"), "tools"),
        (("type: object", "type: list"), "tools"),
        (("{API_KEY: api.example.com}", "{API_KEY: '*.example.com'}"), "secrets"),
        (("egress: [api.example.com]", "egress: [10.0.0.1]"), "egress"),
        (("sensitivity: read", "sensitivity: host_exec"), "tools"),
        (("requirements: [httpx]", "requirements: ['--index-url http://x']"), "requirements"),
        (("description: Forecasts.\n", "description: Forecasts.\nextra: 1\n"), "plugin.yaml"),
    ],
)
def test_a_bad_manifest_names_the_part_to_fix(change: tuple[str, str], field: str):
    with pytest.raises(PluginValidationError) as raised:
        _ = validate_files(bundle(MANIFEST.replace(*change)))
    assert raised.value.field == field


@pytest.mark.parametrize("path", ["../escape.py", "/abs.py", "a//b.py", "a/./b.py"])
def test_a_path_outside_the_plugin_is_refused(path: str):
    with pytest.raises(PluginValidationError):
        _ = validate_files({**bundle(), path: b""})


def test_a_tool_name_too_long_for_a_provider_is_refused():
    long_tool = MANIFEST.replace("- name: forecast", "- name: " + "f" * 40)
    with pytest.raises(PluginValidationError):
        _ = validate_files(bundle(long_tool.replace("name: weather", "name: " + "w" * 20)))


# --- drafts, publishing and history --------------------------------------------


async def test_an_agents_plugin_is_a_draft_until_published(store: PluginStore):
    view = await store.write_draft(OWNER, "weather", bundle(), source="agent")
    assert (view.source, view.published, view.active) == ("agent", False, None)
    assert await store.published(OWNER) == []

    view = await store.publish(OWNER, view.id)
    assert view.published and view.draft is None
    [(_published, files)] = await store.published(OWNER)
    assert files["weather.py"] == CODE.encode()


async def test_editing_a_published_plugin_drafts_beside_what_runs(store: PluginStore):
    view = await store.write_draft(OWNER, "weather", bundle(), source="agent")
    _ = await store.publish(OWNER, view.id)

    _ = await store.write_draft(
        OWNER, "weather", {"weather.py": b"def forecast(city): return 1\n"}, source="agent"
    )

    assert (await store.files(OWNER, view.id, "active"))["weather.py"] == CODE.encode()
    assert (await store.files(OWNER, view.id, "draft"))["plugin.yaml"] == MANIFEST.encode()


async def test_a_draft_must_keep_its_own_name(store: PluginStore):
    _ = await store.write_draft(OWNER, "weather", bundle(), source="agent")
    renamed = MANIFEST.replace("name: weather", "name: climate")
    with pytest.raises(PluginValidationError):
        _ = await store.write_draft(
            OWNER, "weather", {"plugin.yaml": renamed.encode()}, source="agent"
        )


async def test_discarding_a_never_published_draft_deletes_the_plugin(store: PluginStore):
    view = await store.write_draft(OWNER, "weather", bundle(), source="agent")
    assert await store.discard_draft(OWNER, view.id) is None
    assert await store.list(OWNER) == []


async def test_rolling_back_publishes_an_earlier_version_and_keeps_the_draft(store: PluginStore):
    view = await store.write_draft(OWNER, "weather", bundle(), source="agent")
    _ = await store.publish(OWNER, view.id)
    newer = {"weather.py": b"def forecast(city): return 2\n"}
    _ = await store.write_draft(OWNER, "weather", newer, source="agent")
    _ = await store.publish(OWNER, view.id)
    _ = await store.write_draft(
        OWNER, "weather", {"weather.py": b"def forecast(city): return 3\n"}, source="agent"
    )

    _ = await restore(store, OWNER, view.id, 1)

    assert (await store.files(OWNER, view.id, "active"))["weather.py"] == CODE.encode()
    assert b"return 3" in (await store.files(OWNER, view.id, "draft"))["weather.py"]
    assert store.revisions is not None
    history = await store.revisions.history(OWNER, "plugin", view.id)
    assert [entry.note for entry in history] == ["restored version 1", "published", "published"]


# --- secrets ----------------------------------------------------------------------


async def test_a_secret_is_set_for_its_declared_host_and_never_listed(store: PluginStore):
    view = await store.write_draft(OWNER, "weather", bundle(), source="agent")
    [status] = await store.secrets(OWNER, view.id)
    assert (status.name, status.domain, status.is_set) == ("API_KEY", "api.example.com", False)

    await store.set_secret(OWNER, view.id, "API_KEY", "s3cret")

    [status] = await store.secrets(OWNER, view.id)
    assert status.is_set
    assert "s3cret" not in repr(status)
    assert (await store.secret_values(OWNER, view.id))["API_KEY"].value == "s3cret"


async def test_only_declared_secrets_can_be_set(store: PluginStore):
    view = await store.write_draft(OWNER, "weather", bundle(), source="agent")
    with pytest.raises(PluginValidationError):
        await store.set_secret(OWNER, view.id, "OTHER", "x")
    with pytest.raises(PluginValidationError):
        await store.set_secret(OWNER, view.id, "API_KEY", "x", domain="*.example.com")


# --- the REST surface ---------------------------------------------------------------


def _zip(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for path, blob in files.items():
            archive.writestr(f"weather/{path}", blob)
    return buffer.getvalue()


async def test_an_imported_plugin_is_reviewed_published_and_rolled_back():
    async with client_app() as (client, _app):
        imported = await client.post(
            "/plugins/import", files={"file": ("weather.zip", _zip(bundle()), "application/zip")}
        )
        assert imported.status_code == 201, imported.text
        plugin = json_body(imported)
        draft = obj(plugin["draft"])
        assert obj(arr(draft["tools"])[0])["name"] == "forecast"
        assert plugin["active"] is None

        changes = json_list(await client.get(f"/plugins/{plugin['id']}/revisions/changes"))
        assert {s(obj(c)["path"]) for c in changes} == {"plugin.yaml", "weather.py"}

        published = json_body(await client.post(f"/plugins/{plugin['id']}/publish"))
        assert published["published"] and published["draft"] is None

        files = json_list(await client.get(f"/plugins/{plugin['id']}/files?version=active"))
        assert [obj(f)["path"] for f in files] == ["plugin.yaml", "weather.py"]

        put = await client.put(f"/plugins/{plugin['id']}/secrets/API_KEY", json={"value": "s3cret"})
        assert put.status_code == 204
        secrets = json_list(await client.get(f"/plugins/{plugin['id']}/secrets"))
        assert secrets == [{"name": "API_KEY", "domain": "api.example.com", "is_set": True}]

        history = json_list(await client.get(f"/plugins/{plugin['id']}/revisions"))
        assert [obj(v)["note"] for v in history] == ["published"]


async def test_an_invalid_import_is_a_422_naming_the_field():
    async with client_app() as (client, _app):
        broken = bundle(MANIFEST.replace("entry: weather:forecast", "entry: nope:forecast"))
        response = await client.post(
            "/plugins/import", files={"file": ("weather.zip", _zip(broken), "application/zip")}
        )
        assert response.status_code == 422
