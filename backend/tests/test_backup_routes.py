"""The backup REST surface (`BACKUP-1..2`): export, re-import, and what it reports."""

from __future__ import annotations

import httpx

from tests._helpers import client_app
from tests._json import arr, json_body, json_list, obj, parse_json, s

SECRET = "correct horse battery staple"


async def _memory(client: httpx.AsyncClient, content: str) -> None:
    resp = await client.post("/memory", json={"content": content})
    assert resp.status_code == 201


async def test_manifest_is_null_until_the_first_export():
    async with client_app() as (client, _):
        assert parse_json((await client.get("/backup/manifest")).content) is None

        exported = await client.post("/backup/export", json={"secret": SECRET})
        assert exported.status_code == 200

        manifest = parse_json((await client.get("/backup/manifest")).content)
        assert obj(manifest)["createdAt"] == obj(json_body(exported)["manifest"])["createdAt"]


async def test_contents_reports_the_discovered_groups():
    async with client_app() as (client, _):
        await _memory(client, "Deploys go out on Thursdays")

        contents = json_body(await client.get("/backup/contents"))
        assert {"memories", "skills", "settings", "preferences"} <= set(
            map(s, arr(contents["sections"]))
        )
        assert (
            dict((obj(i)["name"], obj(i)["count"]) for i in arr(contents["items"]))["memories"] == 1
        )


async def test_export_then_reimport_is_idempotent():
    async with client_app() as (client, _):
        await _memory(client, "Deploys go out on Thursdays")
        envelope = json_body(await client.post("/backup/export", json={"secret": SECRET}))[
            "envelope"
        ]
        assert "Thursdays" not in str(envelope)

        first = json_body(
            await client.post("/backup/import", json={"secret": SECRET, "envelope": envelope})
        )
        # The rows are already here — a restore onto its own source adds nothing.
        assert obj(first["imported"])["memories"] == 0
        assert obj(first["skipped"])["memories"] == 1
        assert len(json_list(await client.get("/memory"))) == 1


async def test_a_wrong_secret_is_400_not_an_auth_failure():
    async with client_app() as (client, _):
        await _memory(client, "A fact")
        envelope = json_body(await client.post("/backup/export", json={"secret": SECRET}))[
            "envelope"
        ]

        # 400, deliberately: 401/423 would have the frontend client drop the session and
        # bounce to login over a mistyped recovery passphrase.
        wrong = await client.post("/backup/import", json={"secret": "nope", "envelope": envelope})
        assert wrong.status_code == 400


async def test_a_file_that_is_not_a_backup_is_422():
    async with client_app() as (client, _):
        resp = await client.post(
            "/backup/import", json={"secret": SECRET, "envelope": {"hello": "world"}}
        )
        assert resp.status_code == 422


async def test_export_requires_a_secret():
    async with client_app() as (client, _):
        assert (await client.post("/backup/export", json={"secret": ""})).status_code == 422
