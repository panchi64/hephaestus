"""The /agents surface."""

from __future__ import annotations

from ._helpers import client_app
from ._json import json_body, json_list, obj, s

_BODY = {"name": "scout", "description": "Looks around.", "instructions": "Look around."}


async def test_the_builtins_are_listed_read_only():
    async with client_app() as (client, _app):
        listed = json_list(await client.get("/agents"))
    by_id = {s(obj(a)["id"]): obj(a) for a in listed}
    assert by_id["builtin:default"]["editable"] is False
    assert by_id["builtin:explorer"]["source"] == "builtin"
    assert all(not obj(a)["editable"] for a in listed)


async def test_a_builtin_is_fetched_by_its_id():
    async with client_app() as (client, _app):
        got = await client.get("/agents/builtin:reviewer")
        assert got.status_code == 200
        assert json_body(got)["name"] == "reviewer"
        assert (await client.get("/agents/builtin:nope")).status_code == 404


async def test_creating_makes_an_inactive_draft():
    async with client_app() as (client, _app):
        made = await client.post(
            "/agents", json={**_BODY, "tools": ["files"], "deny_tools": ["shell"]}
        )
        assert made.status_code == 201
        body = json_body(made)
        assert body["published"] is False
        assert body["source"] == "user"
        assert body["editable"] is True
        assert body["tools"] == ["files"]
        assert body["deny_tools"] == ["shell"]
        fetched = json_body(await client.get(f"/agents/{s(body['id'])}"))
        assert body["id"] == fetched["id"]


async def test_the_class_catalog_marks_the_classes_an_agent_can_take():
    async with client_app() as (client, _app):
        listed = json_list(await client.get("/agents/classes"))
    assignable = {s(obj(c)["key"]): obj(c)["assignable"] for c in listed}
    assert assignable["tripodes"] is True
    assert assignable["talos"] is False
    assert assignable["keledones"] is False
    assert all(s(obj(c)["role"]) for c in listed)


async def test_an_agent_takes_the_class_it_is_given():
    async with client_app() as (client, _app):
        made = json_body(await client.post("/agents", json={**_BODY, "agent_class": "Kourai"}))
        assert made["agent_class"] == "kourai"
        changed = await client.patch(f"/agents/{s(made['id'])}", json={"agent_class": "khryseos"})
        assert json_body(changed)["agent_class"] == "khryseos"


async def test_an_agent_cannot_take_a_reserved_class():
    async with client_app() as (client, _app):
        for reserved in ("talos", "Kēlēdones"):
            resp = await client.post("/agents", json={**_BODY, "agent_class": reserved})
            assert resp.status_code == 422
            assert obj(json_body(resp)["detail"])["field"] == "agent_class"


async def test_bad_input_is_a_422_naming_the_field():
    async with client_app() as (client, _app):
        for body, field in (
            ({**_BODY, "name": "default"}, "name"),
            ({**_BODY, "description": ""}, "description"),
            ({**_BODY, "wall_clock_timeout_s": 0}, "wall_clock_timeout_s"),
        ):
            resp = await client.post("/agents", json=body)
            assert resp.status_code == 422
            assert obj(json_body(resp)["detail"])["field"] == field
        assert (await client.post("/agents", json={**_BODY, "workspace": "x"})).status_code == 422


async def test_publishing_and_unpublishing():
    async with client_app() as (client, _app):
        agent_id = s(json_body(await client.post("/agents", json=_BODY))["id"])
        live = json_body(await client.post(f"/agents/{agent_id}/publish"))
        assert live["published"] is True and live["active"] is True
        down = json_body(await client.post(f"/agents/{agent_id}/unpublish"))
        assert down["published"] is False and down["active"] is False


async def test_patching_changes_what_is_sent_and_clears_an_explicit_null():
    async with client_app() as (client, _app):
        agent_id = s(json_body(await client.post("/agents", json={**_BODY, "model": "m"}))["id"])
        patched = await client.patch(
            f"/agents/{agent_id}", json={"description": "Looks harder.", "model": None}
        )
        assert patched.status_code == 200
        body = json_body(patched)
        assert body["description"] == "Looks harder."
        assert body["model"] is None
        assert body["instructions"] == "Look around."

        bad = await client.patch(f"/agents/{agent_id}", json={"name": "default"})
        assert bad.status_code == 422


async def test_deleting():
    async with client_app() as (client, _app):
        agent_id = s(json_body(await client.post("/agents", json=_BODY))["id"])
        assert (await client.delete(f"/agents/{agent_id}")).status_code == 204
        assert (await client.get(f"/agents/{agent_id}")).status_code == 404


async def test_a_stored_agent_shadows_a_builtin_of_the_same_name():
    async with client_app() as (client, _app):
        agent_id = s(
            json_body(await client.post("/agents", json={**_BODY, "name": "reviewer"}))["id"]
        )
        _ = await client.post(f"/agents/{agent_id}/publish")
        listed = {s(obj(a)["id"]): obj(a) for a in json_list(await client.get("/agents"))}
        assert listed[agent_id]["active"] is True
        assert listed["builtin:reviewer"]["active"] is False


async def test_an_exported_agent_imports_back_as_a_draft():
    async with client_app() as (client, _app):
        made = json_body(
            await client.post(
                "/agents",
                json={
                    **_BODY,
                    "tools": ["files"],
                    "deny_tools": ["shell"],
                    "permission_ceiling": "plan",
                    "workspace": "own",
                    "wall_clock_timeout_s": 600.0,
                    "can_delegate": True,
                },
            )
        )
        exported = await client.get(f"/agents/{s(made['id'])}/export")
        assert exported.status_code == 200
        assert 'filename="scout.md"' in exported.headers["content-disposition"]
        _ = await client.delete(f"/agents/{s(made['id'])}")

        imported = await client.post(
            "/agents/import", files={"file": ("scout.md", exported.content, "text/markdown")}
        )
        assert imported.status_code == 201
        body = json_body(imported)
        assert body["published"] is False
        assert {k: body[k] for k in ("name", "tools", "deny_tools", "workspace")} == {
            "name": "scout",
            "tools": ["files"],
            "deny_tools": ["shell"],
            "workspace": "own",
        }
        assert body["permission_ceiling"] == "plan"
        assert body["wall_clock_timeout_s"] == 600.0
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
        assert obj(json_body(resp)["detail"])["field"] == "file"


async def test_an_agent_is_edited_as_its_whole_file():
    async with client_app() as (client, _app):
        agent_id = s(json_body(await client.post("/agents", json=_BODY))["id"])
        document = s(json_body(await client.get(f"/agents/{agent_id}/document"))["content"])
        assert document.startswith("---\nname: scout\n")
        edited = document.replace("Look around.", "Look harder.").replace(
            "description: Looks around.", "description: Looks around.\npermission: plan"
        )
        saved = await client.put(f"/agents/{agent_id}/document", json={"content": edited})
        assert saved.status_code == 200
        assert json_body(saved)["instructions"] == "Look harder."
        assert json_body(saved)["permission_ceiling"] == "plan"
        broken = await client.put(f"/agents/{agent_id}/document", json={"content": "nope"})
        assert broken.status_code == 422
        assert obj(json_body(broken)["detail"])["field"] == "file"


async def test_a_builtin_reads_as_a_document_but_cannot_be_written():
    async with client_app() as (client, _app):
        got = await client.get("/agents/builtin:explorer/document")
        assert got.status_code == 200
        put = await client.put("/agents/builtin:explorer/document", json={"content": "x"})
        assert put.status_code == 404
