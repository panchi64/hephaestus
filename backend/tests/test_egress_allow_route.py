"""The operator's route for allowing a refused domain from a command card."""

from __future__ import annotations

from routes import deps
from routes.deps import OPERATOR_ID, app_state
from services.workspace import scratch_key

from ._helpers import client_app
from ._json import arr, json_body, s


async def test_a_grant_lands_on_the_threads_own_scratch_key():
    async with client_app() as (client, app):
        conversation = await deps.store(app).create_conversation(OPERATOR_ID)

        resp = await client.post(
            f"/operator/conversations/{conversation}/egress",
            json={"domain": "https://Files.Example.org/x"},
        )

        assert resp.status_code == 200, resp.text
        assert "files.example.org" in [s(d) for d in arr(json_body(resp)["allowed"])]
        granted = await deps.egress(app).allowed_for(scratch_key(conversation))
        assert "files.example.org" in granted


async def test_a_sub_agent_thread_grants_the_scratch_it_shares_with_its_parent():
    async with client_app() as (client, app):
        store = deps.store(app)
        parent = await store.create_conversation(OPERATOR_ID)
        child = await store.create_conversation(
            OPERATOR_ID, parent_id=parent, workspace_key=f"{parent}/s-ab12", ephemeral=True
        )

        resp = await client.post(
            f"/operator/conversations/{child}/egress", json={"domain": "api.example.net"}
        )

        assert resp.status_code == 200, resp.text
        policy = deps.egress(app)
        assert "api.example.net" in await policy.allowed_for(parent)
        assert "api.example.net" not in await policy.allowed_for(child)


async def test_an_unknown_conversation_is_404():
    async with client_app() as (client, _app):
        resp = await client.post(
            "/operator/conversations/nope/egress", json={"domain": "api.example.net"}
        )
        assert resp.status_code == 404


async def test_another_owners_conversation_is_404():
    async with client_app() as (client, app):
        theirs = await deps.store(app).create_conversation("someone-else")
        resp = await client.post(
            f"/operator/conversations/{theirs}/egress", json={"domain": "api.example.net"}
        )
        assert resp.status_code == 404
        assert "api.example.net" not in await deps.egress(app).allowed_for(scratch_key(theirs))


async def test_an_invalid_domain_is_refused_and_grants_nothing():
    async with client_app() as (client, app):
        conversation = await deps.store(app).create_conversation(OPERATOR_ID)
        for bad in ("", "10.0.0.1", "*.com", "a@evil.example.com", "*"):
            resp = await client.post(
                f"/operator/conversations/{conversation}/egress", json={"domain": bad}
            )
            assert resp.status_code == 422, (bad, resp.text)
        allowed = await deps.egress(app).allowed_for(scratch_key(conversation))
        assert "evil.example.com" not in allowed


async def test_no_api_scope_claims_the_route():
    async with client_app() as (_client, app):
        table = app_state(app).api_scope_table
        assert table.scope_for_path("/operator/conversations/x/egress", "POST") is None


async def test_an_api_token_cannot_reach_the_route():
    async with client_app(auth_enabled=True, passphrase=None) as (client, app):
        _ = await client.post("/setup", json={"password": "the-password"})
        conversation = await deps.store(app).create_conversation(OPERATOR_ID)
        issued = await client.post(
            "/tokens", json={"label": "ci", "scopes": ["chat", "projects", "tasks"]}
        )
        token = s(json_body(issued)["token"])
        client.cookies.clear()

        resp = await client.post(
            f"/operator/conversations/{conversation}/egress",
            json={"domain": "api.example.net"},
            headers={"Authorization": f"Bearer {token}"},
        )

        assert resp.status_code == 403
        assert "api.example.net" not in await deps.egress(app).allowed_for(
            scratch_key(conversation)
        )
