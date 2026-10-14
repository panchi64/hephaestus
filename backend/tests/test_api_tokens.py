"""Outbound service credentials — encryption at rest, write-only routes, lock-aware
degrade, and the on-change hook. No network."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from sqlmodel import Session, select

from core.db import init_db, make_engine
from core.exceptions import NotFoundError
from core.vault import Vault
from models.service_credential import ServiceCredential
from services.credential_store import CredentialStore
from tests._helpers import client_app
from tests._json import json_body, json_list, obj, s

OWNER = "operator"


async def _make():
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    vault = Vault(Path(tempfile.mkdtemp()) / "keyfile.json")
    await vault.setup("pw")
    return CredentialStore(engine, vault), engine, vault


async def test_set_get_roundtrip_and_status():
    store, _engine, _vault = await _make()
    await store.set_key(OWNER, "google_oauth", "goog-secret-123")
    assert await store.get_secret(OWNER, "google_oauth") == "goog-secret-123"
    assert await store.status(OWNER) == {"google_oauth": True}
    # An unset service has no key.
    assert await store.get_secret(OWNER, "microsoft_oauth") is None


async def test_set_is_an_upsert():
    store, _engine, _vault = await _make()
    await store.set_key(OWNER, "microsoft_oauth", "first")
    await store.set_key(OWNER, "microsoft_oauth", "second")
    assert await store.get_secret(OWNER, "microsoft_oauth") == "second"


async def test_key_is_encrypted_at_rest():
    store, engine, vault = await _make()
    await store.set_key(OWNER, "google_oauth", "oauth-secret-xyz")
    with Session(engine) as session:
        row = session.exec(select(ServiceCredential)).one()
    assert row.api_key_enc is not None
    assert "oauth-secret-xyz" not in row.api_key_enc  # sealed, not plaintext
    assert vault.decrypt_str(row.api_key_enc) == "oauth-secret-xyz"


async def test_clear_removes_the_key():
    store, _engine, _vault = await _make()
    await store.set_key(OWNER, "microsoft_oauth", "k")
    await store.clear_key(OWNER, "microsoft_oauth")
    assert await store.get_secret(OWNER, "microsoft_oauth") is None
    assert await store.status(OWNER) == {}
    # Setting an empty key is also a clear.
    await store.set_key(OWNER, "microsoft_oauth", "k")
    await store.set_key(OWNER, "microsoft_oauth", "")
    assert await store.get_secret(OWNER, "microsoft_oauth") is None


async def test_unknown_service_rejected():
    store, _engine, _vault = await _make()
    with pytest.raises(NotFoundError):
        await store.set_key(OWNER, "not-a-service", "k")
    with pytest.raises(NotFoundError):
        await store.clear_key(OWNER, "not-a-service")


async def test_locked_vault_yields_none_not_a_crash():
    store, _engine, vault = await _make()
    await store.set_key(OWNER, "google_oauth", "secret")
    vault.lock()
    # Consumers reading a key at boot must degrade, never raise.
    assert await store.get_secret(OWNER, "google_oauth") is None


async def test_on_change_fires_after_writes():
    store, _engine, _vault = await _make()
    hits: list[int] = []
    store.on_change(lambda: hits.append(1))
    await store.set_key(OWNER, "microsoft_oauth", "k")
    await store.clear_key(OWNER, "microsoft_oauth")
    assert len(hits) == 2


async def test_credentials_route_set_list_clear_and_never_leaks_key():
    async with client_app() as (client, _app):
        resp = await client.get("/credentials")
        assert resp.status_code == 200
        by_service = {s(obj(c)["service"]): obj(c) for c in json_list(resp)}
        assert {"google_oauth", "microsoft_oauth"} <= by_service.keys()
        assert by_service["google_oauth"]["has_key"] is False

        resp = await client.put("/credentials/google_oauth", json={"api_key": "sk-secret"})
        assert resp.status_code == 200
        assert json_body(resp)["has_key"] is True
        assert "sk-secret" not in resp.text  # the key is never echoed back

        resp = await client.get("/credentials")
        assert {s(obj(c)["service"]): obj(c)["has_key"] for c in json_list(resp)}[
            "google_oauth"
        ] is True

        assert (await client.put("/credentials/bogus", json={"api_key": "x"})).status_code == 404

        resp = await client.delete("/credentials/google_oauth")
        assert resp.status_code == 200 and json_body(resp)["has_key"] is False
