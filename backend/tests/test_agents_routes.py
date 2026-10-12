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
