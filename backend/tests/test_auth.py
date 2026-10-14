"""Auth: first-run setup, login, the global gate, lock, and dual transport."""

from __future__ import annotations

from ._helpers import client_app
from ._json import json_body, s


async def test_setup_then_access_then_lock_cycle():
    async with client_app(auth_enabled=True, passphrase=None) as (client, _app):
        # Fresh: not initialized, and feature endpoints are gated.
        status = json_body(await client.get("/auth/status"))
        assert status == {
            "initialized": False,
            "unlocked": False,
            "auth_enabled": True,
            "db_missing": False,
        }
        assert (await client.get("/runs/whatever")).status_code == 401

        # First-run setup chooses the password and returns a session.
        setup = await client.post("/setup", json={"password": "correct horse"})
        assert setup.status_code == 200
        assert json_body(setup)["token"]

        # The cookie now authorizes feature endpoints (404 = reached the handler).
        assert (await client.get("/runs/whatever")).status_code == 404
        assert (await client.get("/auth/status")).json()["unlocked"] is True

        # Lock wipes the key and revokes sessions → gated again.
        assert (await client.post("/auth/lock")).status_code == 200
        assert (await client.get("/runs/whatever")).status_code == 401


async def test_setup_is_one_time_and_password_min_length():
    async with client_app(auth_enabled=True, passphrase=None) as (client, _app):
        assert (await client.post("/setup", json={"password": "short"})).status_code == 422
        assert (await client.post("/setup", json={"password": "long enough"})).status_code == 200
        # already set up
        assert (await client.post("/setup", json={"password": "long enough"})).status_code == 409


async def test_login_rejects_wrong_password_and_accepts_right():
    async with client_app(auth_enabled=True, passphrase=None) as (client, _app):
        _ = await client.post("/setup", json={"password": "the-password"})
        _ = await client.post("/auth/lock")

        assert (await client.post("/auth/login", json={"password": "nope"})).status_code == 401
        ok = await client.post("/auth/login", json={"password": "the-password"})
        assert ok.status_code == 200
        assert (await client.get("/runs/whatever")).status_code == 404


async def test_bearer_token_authorizes_without_cookie():
    async with client_app(auth_enabled=True, passphrase=None) as (client, _app):
        token = s(
            json_body(await client.post("/setup", json={"password": "the-password"}))["token"]
        )
        client.cookies.clear()  # drop the cookie; rely on the bearer token

        assert (await client.get("/runs/whatever")).status_code == 401
        headers = {"Authorization": f"Bearer {token}"}
        assert (await client.get("/runs/whatever", headers=headers)).status_code == 404


async def test_lock_and_logout_require_authentication():
    # The gate must not exempt the state-changing /auth endpoints: an
    # unauthenticated caller can't lock the vault or revoke sessions (DoS).
    async with client_app(auth_enabled=True, passphrase=None) as (client, _app):
        _ = await client.post("/setup", json={"password": "the-password"})
        client.cookies.clear()  # become unauthenticated

        assert (await client.post("/auth/lock")).status_code == 401
        assert (await client.post("/auth/logout")).status_code == 401
        # The lock attempt was rejected at the gate — the vault is still unlocked.
        assert (await client.get("/auth/status")).json()["unlocked"] is True


async def test_auth_disabled_still_requires_unlock():
    # auth off + a passphrase ⇒ unlocked at boot, no token needed.
    async with client_app(auth_enabled=False, passphrase="dev-pass") as (client, _app):
        assert (await client.get("/runs/whatever")).status_code == 404
        # locking still blocks until re-unlocked, even with auth off.
        _ = await client.post("/auth/lock")
        assert (await client.get("/runs/whatever")).status_code == 423


async def test_logout_locks_the_vault():
    # Logging out zeroes the key, it does not just drop the cookie: the DEK is
    # process-global, so a logout that left it resident would keep every decrypted
    # secret, PTY, and secret-vault session alive behind a "logged out" UI.
    async with client_app(auth_enabled=True, passphrase=None) as (client, _app):
        _ = await client.post("/setup", json={"password": "the-password"})
        assert (await client.get("/auth/status")).json()["unlocked"] is True

        assert (await client.post("/auth/logout")).status_code == 200
        assert (await client.get("/auth/status")).json()["unlocked"] is False
        assert (await client.get("/runs/whatever")).status_code == 401

        # ...and the password still unlocks afterwards.
        login = await client.post("/auth/login", json={"password": "the-password"})
        assert login.status_code == 200
        assert (await client.get("/auth/status")).json()["unlocked"] is True


async def test_logout_revokes_every_session_not_just_the_caller():
    # The key is gone process-wide, so a token issued to another client is dead too —
    # it has to be rejected at the gate, not 423 deeper in.
    async with client_app(auth_enabled=True, passphrase=None) as (client, _app):
        setup = await client.post("/setup", json={"password": "the-password"})
        other = {"Authorization": f"Bearer {s(json_body(setup)['token'])}"}

        _ = await client.post("/auth/logout")
        assert (await client.get("/runs/whatever", headers=other)).status_code == 401
