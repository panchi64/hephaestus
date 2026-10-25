"""Inbound scoped API tokens (`AUTH-4`): issue-once, scope enforcement, revocation.

Distinct from `test_api_tokens.py`, which covers the *outbound* service credentials at
`/credentials`. These are the tokens clients authenticate to this API with.
"""

from __future__ import annotations

import asyncio

import httpx
from fastapi import FastAPI
from pydantic import JsonValue
from sqlmodel import Session, select

from core.db import in_session
from models.api_token import ApiToken
from routes.deps import OPERATOR_ID, app_state
from runs import Run

from ._helpers import client_app
from ._json import arr, json_body, obj, parse_json, s


async def _issue(
    client: httpx.AsyncClient, label: str = "laptop CLI", scopes: tuple[str, ...] = ("chat",)
) -> dict[str, JsonValue]:
    resp = await client.post("/tokens", json={"label": label, "scopes": list(scopes)})
    assert resp.status_code == 201, resp.text
    return json_body(resp)


async def _stored(app: FastAPI) -> list[ApiToken]:
    # Through `in_session` like every other reader: a booted app's in-memory engine shares
    # one connection, and its background drainers are using it.
    def work(session: Session) -> list[ApiToken]:
        return list(session.exec(select(ApiToken)).all())

    return await in_session(app_state(app).db_engine, work)


async def test_issue_returns_the_token_once_and_stores_only_a_hash():
    async with client_app() as (client, app):
        issued = await _issue(client)
        token = s(issued["token"])
        assert token.startswith("odyt_")
        assert s(issued["prefix"]) in token
        assert issued["scopes"] == ["chat"]

        # The listing never carries the plaintext back — it is unrecoverable.
        listed = arr(parse_json((await client.get("/tokens")).content))
        assert [obj(row)["id"] for row in listed] == [issued["id"]]
        assert "token" not in obj(listed[0])

        # Nor does the row: only a one-way hash of the secret half.
        rows = await _stored(app)
        assert len(rows) == 1
        assert rows[0].token_hash.startswith("$argon2")
        assert token not in rows[0].token_hash
        assert token.rsplit("_", 1)[1] not in rows[0].token_hash


async def test_issue_rejects_an_unknown_or_empty_scope_set():
    async with client_app() as (client, _app):
        bad = await client.post("/tokens", json={"label": "x", "scopes": ["everything"]})
        assert bad.status_code == 422
        assert "everything" in s(json_body(bad)["detail"])
        assert (await client.post("/tokens", json={"label": "x", "scopes": []})).status_code == 422
        assert (
            await client.post("/tokens", json={"label": " ", "scopes": ["chat"]})
        ).status_code == 422


async def test_scopes_catalog_never_covers_credential_or_host_surfaces():
    # Deny-by-default is the guarantee: no token can mint another token, read the
    # operator's secrets, or drive a backup. The table is per-app, assembled from
    # core claims + every enabled manifest's — so this asserts the real one.
    async with client_app() as (_client, app):
        table = app_state(app).api_scope_table
        for path in ("/tokens", "/tokens/abc", "/credentials", "/vault", "/backup"):
            assert table.scope_for_path(path) is None
        # Opening a native dialog on the operator's machine — and reading back whatever
        # absolute path came out — is never reachable with a token. Neither is the other
        # half of that surface: launching an application on a path a caller named.
        assert table.scope_for_path("/host/file-picker") is None
        assert table.scope_for_path("/host/file-picker", "POST") is None
        assert table.scope_for_path("/host/open", "POST") is None
        assert table.scope_for_path("/models/roles") == "models"
        # The `models` scope is described to the operator as read-only, so it has to be
        # one: creating an endpoint and rebinding a role would let a token route every
        # future turn through an inference server of its choosing.
        assert table.scope_for_path("/models/endpoints", "POST") is None
        assert table.scope_for_path("/models/roles/main", "PUT") is None
        assert table.scope_for_path("/models/endpoints/abc", "DELETE") is None
        assert table.scope_for_path("/models/endpoints", "GET") == "models"


async def test_token_authenticates_in_scope_and_is_refused_out_of_scope():
    async with client_app(auth_enabled=True, passphrase=None) as (client, _app):
        _ = await client.post("/setup", json={"password": "the-password"})
        token = s((await _issue(client, scopes=("chat",)))["token"])
        client.cookies.clear()  # drop the operator session; rely on the token alone

        headers = {"Authorization": f"Bearer {token}"}
        # In scope: reaches the handler (404 = the run doesn't exist).
        assert (await client.get("/runs/whatever", headers=headers)).status_code == 404
        # Out of scope: a real token, but not for this surface.
        denied = await client.get("/memory", headers=headers)
        assert denied.status_code == 403
        # Unclaimed by every scope — a token can't manage tokens.
        assert (await client.get("/tokens", headers=headers)).status_code == 403


async def test_a_token_tailing_a_run_is_not_the_operator_watching_it():
    async with client_app(auth_enabled=True, passphrase=None) as (client, app):
        _ = await client.post("/setup", json={"password": "the-password"})
        token = s((await _issue(client, scopes=("chat",)))["token"])
        client.cookies.clear()
        release = asyncio.Event()

        async def orch(_run: Run) -> None:
            _ = await release.wait()

        run = app_state(app).runs.submit(kind="chat", owner_id=OPERATOR_ID, orchestrator=orch)
        tail = asyncio.create_task(
            client.get(f"/runs/{run.id}/events", headers={"Authorization": f"Bearer {token}"})
        )
        while run.stream.subscriber_count == 0:
            await asyncio.sleep(0.01)

        assert run.stream.watcher_count == 0
        release.set()
        await run.wait()
        _ = await tail


async def test_revoked_token_is_refused_even_after_it_has_been_used():
    async with client_app(auth_enabled=True, passphrase=None) as (client, _app):
        _ = await client.post("/setup", json={"password": "the-password"})
        issued = await _issue(client, scopes=("chat",))
        headers = {"Authorization": f"Bearer {s(issued['token'])}"}
        assert (await client.get("/runs/whatever", headers=headers)).status_code == 404

        revoked = await client.delete(f"/tokens/{s(issued['id'])}")
        assert revoked.status_code == 200
        assert json_body(revoked)["revoked_at"] is not None
        # The gate's in-memory verification cache is dropped on revoke, so the very next
        # request is refused rather than the one after a restart.
        assert (await client.get("/runs/whatever", headers=headers)).status_code == 401
        assert (await client.delete("/tokens/does-not-exist")).status_code == 404


async def test_token_authentication_attempts_are_rate_limited():
    async with client_app(auth_enabled=True, passphrase=None) as (client, app):
        _ = await client.post("/setup", json={"password": "the-password"})
        client.cookies.clear()

        # A guess can never hit the verification cache, so every one spends a bucket token.
        headers = {"Authorization": "Bearer odyt_deadbeef_not-a-real-secret"}
        statuses = [(await client.get("/runs/x", headers=headers)).status_code for _ in range(15)]
        assert statuses[0] == 401
        assert 429 in statuses
        throttled = await client.get("/runs/x", headers=headers)
        assert throttled.status_code == 429
        assert int(throttled.headers["retry-after"]) >= 1

        # A malformed credential is refused at the gate without consuming the throttle or
        # touching the store — it isn't token-shaped at all.
        app.state.auth_attempt_limiter = None
        assert (
            await client.get("/runs/x", headers={"Authorization": "Bearer nonsense"})
        ).status_code == 401


async def test_login_attempts_are_rate_limited():
    async with client_app(auth_enabled=True, passphrase=None) as (client, _app):
        _ = await client.post("/setup", json={"password": "the-password"})
        _ = await client.post("/auth/lock")

        statuses = [
            (await client.post("/auth/login", json={"password": "wrong"})).status_code
            for _ in range(15)
        ]
        assert statuses[0] == 401
        assert 429 in statuses


async def test_operator_session_paths_are_unchanged_by_token_auth():
    # The cookie and bearer session must still authenticate exactly as before, and must
    # not be scope-limited the way a token is.
    async with client_app(auth_enabled=True, passphrase=None) as (client, _app):
        session = s(
            json_body(await client.post("/setup", json={"password": "the-password"}))["token"]
        )
        assert (await client.get("/runs/whatever")).status_code == 404  # cookie
        assert (await client.get("/memory")).status_code == 200
        assert (await client.get("/tokens")).status_code == 200

        client.cookies.clear()
        headers = {"Authorization": f"Bearer {session}"}
        assert (await client.get("/runs/whatever", headers=headers)).status_code == 404
        assert (await client.get("/tokens", headers=headers)).status_code == 200
