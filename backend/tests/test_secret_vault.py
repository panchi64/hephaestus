"""The secrets manager (`VAULT-1`): its own lock, memory-only, re-locked by a restart."""

from __future__ import annotations

import pytest
from sqlalchemy import inspect
from sqlmodel import Session, select

from core.db import init_db, make_engine
from core.vault import Vault
from models.secret import SecretEntry
from services.secret_vault import (
    SecretVaultAlreadyConfigured,
    SecretVaultLocked,
    SecretVaultNotConfigured,
    SecretVaultService,
)

OWNER = "operator"
LOGIN_PASSWORD = "login-password"
VAULT_PASSPHRASE = "vault-passphrase"


async def _fixture(tmp_path, **kwargs):
    """A booted engine + an unlocked login vault + a fresh secrets manager over both."""
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    vault = Vault(tmp_path / "keyfile.json")
    await vault.setup(LOGIN_PASSWORD)
    return engine, vault, SecretVaultService(engine, vault, **kwargs)


async def _configured(tmp_path, **kwargs):
    engine, vault, service = await _fixture(tmp_path, **kwargs)
    await service.configure(OWNER, VAULT_PASSPHRASE)
    return engine, vault, service


# --- lifecycle ---------------------------------------------------------------------


async def test_starts_unconfigured_and_locked(tmp_path):
    _, _, service = await _fixture(tmp_path)
    status = await service.status(OWNER)
    assert not status.configured and not status.unlocked
    with pytest.raises(SecretVaultNotConfigured):
        await service.unlock(OWNER, VAULT_PASSPHRASE)


async def test_configure_leaves_it_unlocked_and_refuses_a_second_time(tmp_path):
    _, _, service = await _configured(tmp_path)
    status = await service.status(OWNER)
    assert status.configured and status.unlocked
    with pytest.raises(SecretVaultAlreadyConfigured):
        await service.configure(OWNER, "another-passphrase")


async def test_unlock_requires_its_own_passphrase(tmp_path):
    _, _, service = await _configured(tmp_path)
    service.lock(OWNER)

    # Neither a wrong passphrase nor the *login* password opens this vault: its key is
    # derived from its own passphrase over its own salt.
    assert await service.unlock(OWNER, "wrong") is False
    assert await service.unlock(OWNER, LOGIN_PASSWORD) is False
    assert not (await service.status(OWNER)).unlocked

    assert await service.unlock(OWNER, VAULT_PASSPHRASE) is True
    assert (await service.status(OWNER)).unlocked


async def test_lock_and_logout_both_re_seal(tmp_path):
    _, _, service = await _configured(tmp_path)
    service.lock(OWNER)
    assert not (await service.status(OWNER)).unlocked

    await service.unlock(OWNER, VAULT_PASSPHRASE)
    service.logout()
    assert not (await service.status(OWNER)).unlocked


async def test_idle_session_expires(tmp_path):
    now = [1000.0]
    _, _, service = await _configured(tmp_path, idle_timeout_s=60.0, clock=lambda: now[0])
    await service.create(OWNER, name="db", password="pw")

    # A read inside the window slides the deadline forward…
    now[0] += 59.0
    assert len(await service.list_entries(OWNER)) == 1
    now[0] += 59.0
    assert len(await service.list_entries(OWNER)) == 1

    # …and one past it finds the vault closed.
    now[0] += 61.0
    assert not (await service.status(OWNER)).unlocked
    with pytest.raises(SecretVaultLocked):
        await service.list_entries(OWNER)


# --- memory-only unlocked state ----------------------------------------------------


async def test_unlocked_state_is_never_persisted(tmp_path):
    engine, vault, service = await _configured(tmp_path)
    await service.create(OWNER, name="db", username="admin", password="pw")

    # Nothing in the schema records "open": no column of either vault table names it.
    from models.secret import SecretVaultConfig

    columns = {c.name for table in (SecretEntry, SecretVaultConfig) for c in inspect(table).columns}
    assert not {c for c in columns if "unlock" in c or "locked" in c or "session" in c}

    # And a second service over the very same DB + login vault — the process-restart
    # shape — starts locked, because the key only ever lived in the first one's memory.
    restarted = SecretVaultService(engine, vault)
    assert not (await restarted.status(OWNER)).unlocked
    with pytest.raises(SecretVaultLocked):
        await restarted.list_entries(OWNER)

    assert await restarted.unlock(OWNER, VAULT_PASSPHRASE) is True
    assert [e.name for e in await restarted.list_entries(OWNER)] == ["db"]


# --- independence from the login vault ---------------------------------------------


async def test_the_two_locks_are_independent(tmp_path):
    _, vault, service = await _configured(tmp_path)

    # Locking the secrets manager leaves the app itself unlocked.
    service.lock(OWNER)
    assert vault.is_unlocked
    assert not (await service.status(OWNER)).unlocked

    # Unlocking/relocking the *login* vault never opens the secrets manager…
    vault.lock()
    assert await vault.unlock(LOGIN_PASSWORD) is True
    assert not (await service.status(OWNER)).unlocked

    # …and an app lock closes any open vault session with it (the safe direction).
    await service.unlock(OWNER, VAULT_PASSPHRASE)
    vault.lock()
    assert not (await service.status(OWNER)).unlocked


# --- entries -----------------------------------------------------------------------


async def test_entries_round_trip_and_are_double_sealed(tmp_path):
    engine, vault, service = await _configured(tmp_path)
    entry = await service.create(
        OWNER, name="Production DB", username="admin", url="db://x", password="s3cret"
    )
    assert (entry.name, entry.username, entry.password) == ("Production DB", "admin", "s3cret")

    with Session(engine) as session:
        row = session.exec(select(SecretEntry)).one()
    # Nothing readable on disk…
    assert "s3cret" not in row.password_enc and "Production DB" not in row.name_enc
    # …and peeling only the at-rest layer still yields ciphertext, not the secret: the
    # vault's own key is a second, independent layer.
    assert "s3cret" not in vault.decrypt_str(row.password_enc)


async def test_update_and_delete(tmp_path):
    _, _, service = await _configured(tmp_path)
    entry = await service.create(OWNER, name="db", password="old")

    updated = await service.update(OWNER, entry.id, password="new", url="db://y")
    assert (updated.password, updated.url, updated.name) == ("new", "db://y", "db")

    await service.delete(OWNER, entry.id)
    assert await service.list_entries(OWNER) == []


async def test_a_locked_vault_refuses_every_entry_operation(tmp_path):
    _, _, service = await _configured(tmp_path)
    entry = await service.create(OWNER, name="db", password="pw")
    service.lock(OWNER)

    for call in (
        service.list_entries(OWNER),
        service.get(OWNER, entry.id),
        service.create(OWNER, name="another"),
        service.update(OWNER, entry.id, password="x"),
        service.delete(OWNER, entry.id),
    ):
        with pytest.raises(SecretVaultLocked):
            await call
