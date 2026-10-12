"""The /agents surface."""

from __future__ import annotations

from ._helpers import client_app

_BODY = {"name": "scout", "description": "Looks around.", "instructions": "Look around."}


async def test_the_builtins_are_listed_read_only(monkeypatch):
    async with client_app() as (client, _app):
        listed = (await client.get("/agents")).json()
    by_id = {a["id"]: a for a in listed}
    assert by_id["builtin:default"]["editable"] is False
    assert by_id["builtin:explorer"]["source"] == "builtin"
    assert all(not a["editable"] for a in listed)


async def test_a_builtin_is_fetched_by_its_id():
    async with client_app() as (client, _app):
        got = await client.get("/agents/builtin:reviewer")
        assert got.status_code == 200
        assert got.json()["name"] == "reviewer"
        assert (await client.get("/agents/builtin:nope")).status_code == 404


async def test_creating_makes_an_inactive_draft():
    async with client_app() as (client, _app):
        made = await client.post(
            "/agents", json={**_BODY, "tools": ["files"], "deny_tools": ["shell"]}
        )
        assert made.status_code == 201
        body = made.json()
        assert body["published"] is False
        assert body["source"] == "user"
        assert body["editable"] is True
        assert body["tools"] == ["files"]
        assert body["deny_tools"] == ["shell"]
        assert body["id"] == (await client.get(f"/agents/{body['id']}")).json()["id"]


async def test_bad_input_is_a_422_naming_the_field():
    async with client_app() as (client, _app):
        for body, field in (
            ({**_BODY, "name": "default"}, "name"),
            ({**_BODY, "description": ""}, "description"),
            ({**_BODY, "request_limit": 0}, "request_limit"),
        ):
            resp = await client.post("/agents", json=body)
            assert resp.status_code == 422
            assert resp.json()["detail"]["field"] == field
        assert (await client.post("/agents", json={**_BODY, "workspace": "x"})).status_code == 422


async def test_publishing_and_unpublishing():
    async with client_app() as (client, _app):
        agent_id = (await client.post("/agents", json=_BODY)).json()["id"]
        live = (await client.post(f"/agents/{agent_id}/publish")).json()
        assert live["published"] is True and live["active"] is True
        down = (await client.post(f"/agents/{agent_id}/unpublish")).json()
        assert down["published"] is False and down["active"] is False


async def test_patching_changes_what_is_sent_and_clears_an_explicit_null():
    async with client_app() as (client, _app):
        agent_id = (await client.post("/agents", json={**_BODY, "model": "m"})).json()["id"]
        patched = await client.patch(
            f"/agents/{agent_id}", json={"description": "Looks harder.", "model": None}
        )
        assert patched.status_code == 200
        body = patched.json()
        assert body["description"] == "Looks harder."
        assert body["model"] is None
        assert body["instructions"] == "Look around."

        bad = await client.patch(f"/agents/{agent_id}", json={"name": "default"})
        assert bad.status_code == 422


async def test_deleting():
    async with client_app() as (client, _app):
        agent_id = (await client.post("/agents", json=_BODY)).json()["id"]
        assert (await client.delete(f"/agents/{agent_id}")).status_code == 204
        assert (await client.get(f"/agents/{agent_id}")).status_code == 404


async def test_a_stored_agent_shadows_a_builtin_of_the_same_name():
    async with client_app() as (client, _app):
        agent_id = (await client.post("/agents", json={**_BODY, "name": "reviewer"})).json()["id"]
        await client.post(f"/agents/{agent_id}/publish")
        listed = {a["id"]: a for a in (await client.get("/agents")).json()}
        assert listed[agent_id]["active"] is True
        assert listed["builtin:reviewer"]["active"] is False


async def test_an_exported_agent_imports_back_as_a_draft():
    async with client_app() as (client, _app):
        made = (
            await client.post(
                "/agents",
                json={
                    **_BODY,
                    "tools": ["files"],
                    "deny_tools": ["shell"],
                    "permission_ceiling": "plan",
                    "workspace": "own",
                    "request_limit": 12,
                    "can_delegate": True,
                },
            )
        ).json()
        exported = await client.get(f"/agents/{made['id']}/export")
        assert exported.status_code == 200
        assert 'filename="scout.md"' in exported.headers["content-disposition"]
        await client.delete(f"/agents/{made['id']}")

        imported = await client.post(
            "/agents/import", files={"file": ("scout.md", exported.content, "text/markdown")}
        )
        assert imported.status_code == 201
        body = imported.json()
        assert body["published"] is False
        assert {k: body[k] for k in ("name", "tools", "deny_tools", "workspace")} == {
            "name": "scout",
            "tools": ["files"],
            "deny_tools": ["shell"],
            "workspace": "own",
        }
        assert body["permission_ceiling"] == "plan"
        assert body["request_limit"] == 12
        assert body["can_delegate"] is True
        assert body["instructions"] == "Look around."


async def test_a_builtin_exports_as_an_agent_file():
    async with client_app() as (client, _app):
        exported = await client.get("/agents/builtin:reviewer/export")
        assert exported.status_code == 200
        assert exported.text.startswith("---\nname: reviewer\n")


async def test_an_unreadable_import_is_a_422():
    async with client_app() as (client, _app):
        resp = await client.post(
            "/agents/import", files={"file": ("x.md", b"no frontmatter here", "text/markdown")}
        )
        assert resp.status_code == 422
        assert resp.json()["detail"]["field"] == "file"


async def test_an_agent_is_edited_as_its_whole_file():
    async with client_app() as (client, _app):
        agent_id = (await client.post("/agents", json=_BODY)).json()["id"]
        document = (await client.get(f"/agents/{agent_id}/document")).json()["content"]
        assert document.startswith("---\nname: scout\n")
        edited = document.replace("Look around.", "Look harder.").replace(
            "description: Looks around.", "description: Looks around.\npermission: plan"
        )
        saved = await client.put(f"/agents/{agent_id}/document", json={"content": edited})
        assert saved.status_code == 200
        assert saved.json()["instructions"] == "Look harder."
        assert saved.json()["permission_ceiling"] == "plan"
        broken = await client.put(f"/agents/{agent_id}/document", json={"content": "nope"})
        assert broken.status_code == 422
        assert broken.json()["detail"]["field"] == "file"


async def test_a_builtin_reads_as_a_document_but_cannot_be_written():
    async with client_app() as (client, _app):
        got = await client.get("/agents/builtin:explorer/document")
        assert got.status_code == 200
        put = await client.put("/agents/builtin:explorer/document", json={"content": "x"})
        assert put.status_code == 404
