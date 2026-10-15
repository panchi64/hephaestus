"""The secrets-manager REST surface (`VAULT-1`): setup, unlock, lock, and entry CRUD."""

from __future__ import annotations

import httpx

from routes.deps import app_state
from tests._helpers import client_app
from tests._json import json_body, json_list, obj, s

PASSPHRASE = "vault-passphrase"


async def _unlocked(client: httpx.AsyncClient) -> None:
    resp = await client.post("/vault/configure", json={"passphrase": PASSPHRASE})
    assert resp.status_code == 201


async def test_state_walks_unconfigured_to_unlocked():
    async with client_app() as (client, _):
        state = json_body(await client.get("/vault/state"))
        assert state == {"configured": False, "unlocked": False}

        await _unlocked(client)
        assert json_body(await client.get("/vault/state")) == {
            "configured": True,
            "unlocked": True,
        }

        # Configuring twice would strand every stored entry behind a discarded key.
        second = await client.post("/vault/configure", json={"passphrase": "other"})
        assert second.status_code == 409


async def test_lock_then_unlock_round_trip():
    async with client_app() as (client, _):
        await _unlocked(client)

        assert json_body(await client.post("/vault/lock"))["unlocked"] is False
        assert (await client.post("/vault/unlock", json={"passphrase": "wrong"})).status_code == 403
        assert (
            json_body(await client.post("/vault/unlock", json={"passphrase": PASSPHRASE}))[
                "unlocked"
            ]
            is True
        )

        # Logout is the broader teardown — same observable state, different scope.
        assert json_body(await client.post("/vault/logout"))["unlocked"] is False


async def test_unlock_before_configure_is_a_precondition_failure():
    async with client_app() as (client, _):
        resp = await client.post("/vault/unlock", json={"passphrase": PASSPHRASE})
        assert resp.status_code == 409


async def test_entry_crud():
    async with client_app() as (client, _):
        await _unlocked(client)

        created = await client.post(
            "/vault/entries",
            json={
                "name": "Production DB",
                "username": "admin",
                "url": "db://prod",
                "password": "s3cret",
            },
        )
        assert created.status_code == 201
        entry = json_body(created)
        assert entry["name"] == "Production DB" and entry["password"] == "s3cret"

        listed = json_list(await client.get("/vault/entries"))
        assert [obj(e)["id"] for e in listed] == [entry["id"]]

        patched = await client.patch(
            f"/vault/entries/{s(entry['id'])}", json={"password": "rotated"}
        )
        assert json_body(patched)["password"] == "rotated"
        assert json_body(patched)["name"] == "Production DB"

        assert (await client.delete(f"/vault/entries/{s(entry['id'])}")).status_code == 204
        assert json_list(await client.get("/vault/entries")) == []


async def test_locked_vault_answers_409_everywhere():
    async with client_app() as (client, _):
        await _unlocked(client)
        entry_id = s(
            json_body(await client.post("/vault/entries", json={"name": "db", "password": "pw"}))[
                "id"
            ]
        )
        _ = await client.post("/vault/lock")

        assert (await client.get("/vault/entries")).status_code == 409
        assert (await client.post("/vault/entries", json={"name": "x"})).status_code == 409
        assert (
            await client.patch(f"/vault/entries/{entry_id}", json={"password": "x"})
        ).status_code == 409
        assert (await client.delete(f"/vault/entries/{entry_id}")).status_code == 409


async def test_unknown_entry_is_404():
    async with client_app() as (client, _):
        await _unlocked(client)
        assert (await client.delete("/vault/entries/nope")).status_code == 404
        assert (
            await client.patch("/vault/entries/nope", json={"password": "x"})
        ).status_code == 404


async def test_the_service_is_one_singleton_across_requests():
    async with client_app() as (client, app):
        await _unlocked(client)
        first = app_state(app).secret_vault
        _ = await client.get("/vault/entries")
        # A per-request instance would have thrown the unlock away between the two calls.
        assert app_state(app).secret_vault is first
