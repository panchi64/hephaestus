"""Password vault — the operator's secrets manager (`VAULT-1..2`).

Distinct from ``core/vault``: that is the password-derived at-rest key custody that unlocks
the app at login. This is the user-facing place to keep credentials, with its own lock. The
module is named ``secret_vault`` rather than ``vault`` so ``app.py`` can import it plainly
alongside its local ``vault`` handle on the key custody — same reason the ``deps`` accessor
is ``secret_vault()``.

See ``plugins/mail/routes.py`` for why the surface is registered before it exists.

Thin, like every router: it parses, calls ``services/secret_vault``, and maps that layer's
domain errors to status codes — 409 for a locked vault or an unmet configure/unlock
precondition, 403 for a wrong passphrase, 404 for an unknown entry.

**Deliberately not 401/423 here.** Those two codes are spoken for app-wide: the frontend's
API client treats either as *the session* having failed, clears the bearer token, and routes
back to login. A wrong vault passphrase or a locked secrets manager is none of that — the
operator's app session is perfectly fine — so this surface stays off those codes rather than
logging someone out of everything for mistyping one passphrase.

Unlock is not separately rate-limited: every attempt pays two Argon2id hashes (the verifier
check, then the key derivation), which *is* the brute-force cost — the same reasoning that
protects the login vault.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from core.exceptions import NotFoundError
from core.vault import VaultLocked
from routes import deps
from routes.camel import CamelModel
from routes.deps import OPERATOR_ID
from services.secret_vault import (
    SecretEntryView,
    SecretVaultAlreadyConfigured,
    SecretVaultLocked,
    SecretVaultNotConfigured,
    SecretVaultStatus,
)

router = APIRouter(prefix="/vault", tags=["vault"])


class VaultStateOut(BaseModel):
    """Everything the screen needs to decide what to show: a setup prompt, an unlock
    prompt, or the entries."""

    configured: bool
    unlocked: bool


class PassphraseIn(BaseModel):
    passphrase: str = Field(min_length=1)


class EntryIn(BaseModel):
    name: str = Field(min_length=1)
    username: str = ""
    url: str = ""
    password: str = ""


class EntryPatch(BaseModel):
    name: str | None = None
    username: str | None = None
    url: str | None = None
    password: str | None = None


class EntryOut(CamelModel):
    id: str
    name: str
    username: str
    url: str
    password: str
    created_at: datetime
    updated_at: datetime


def _state(status: SecretVaultStatus) -> VaultStateOut:
    return VaultStateOut(configured=status.configured, unlocked=status.unlocked)


def _out(view: SecretEntryView) -> EntryOut:
    return EntryOut(
        id=view.id,
        name=view.name,
        username=view.username,
        url=view.url,
        password=view.password,
        created_at=view.created_at,
        updated_at=view.updated_at,
    )


def _locked() -> HTTPException:
    return HTTPException(status_code=409, detail="the password vault is locked")


@router.get("/state", response_model=VaultStateOut)
async def vault_state(request: Request) -> VaultStateOut:
    return _state(await deps.secret_vault(request).status(OPERATOR_ID))


@router.post("/configure", status_code=201, response_model=VaultStateOut)
async def configure_vault(body: PassphraseIn, request: Request) -> VaultStateOut:
    service = deps.secret_vault(request)
    try:
        await service.configure(OPERATOR_ID, body.passphrase)
    except SecretVaultAlreadyConfigured as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except VaultLocked:
        # The app itself is locked, so there is no DEK to seal the wrapped key with.
        raise _locked() from None
    return _state(await service.status(OPERATOR_ID))


@router.post("/unlock", response_model=VaultStateOut)
async def unlock_vault(body: PassphraseIn, request: Request) -> VaultStateOut:
    service = deps.secret_vault(request)
    try:
        opened = await service.unlock(OPERATOR_ID, body.passphrase)
    except SecretVaultNotConfigured as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    if not opened:
        raise HTTPException(status_code=403, detail="invalid vault passphrase")
    return _state(await service.status(OPERATOR_ID))


@router.post("/lock", response_model=VaultStateOut)
async def lock_vault(request: Request) -> VaultStateOut:
    service = deps.secret_vault(request)
    service.lock(OPERATOR_ID)
    return _state(await service.status(OPERATOR_ID))


@router.post("/logout", response_model=VaultStateOut)
async def logout_vault(request: Request) -> VaultStateOut:
    """End the vault session outright. Broader than lock — it closes every session this
    process holds, which is also what an app-level lock/logout does to the vault."""
    service = deps.secret_vault(request)
    service.logout()
    return _state(await service.status(OPERATOR_ID))


@router.get("/entries", response_model=list[EntryOut])
async def list_entries(request: Request) -> list[EntryOut]:
    try:
        views = await deps.secret_vault(request).list_entries(OPERATOR_ID)
    except SecretVaultLocked:
        raise _locked() from None
    return [_out(v) for v in views]


@router.post("/entries", status_code=201, response_model=EntryOut)
async def create_entry(body: EntryIn, request: Request) -> EntryOut:
    try:
        view = await deps.secret_vault(request).create(
            OPERATOR_ID,
            name=body.name,
            username=body.username,
            url=body.url,
            password=body.password,
        )
    except SecretVaultLocked:
        raise _locked() from None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return _out(view)


@router.patch("/entries/{entry_id}", response_model=EntryOut)
async def update_entry(entry_id: str, body: EntryPatch, request: Request) -> EntryOut:
    try:
        view = await deps.secret_vault(request).update(
            OPERATOR_ID,
            entry_id,
            name=body.name,
            username=body.username,
            url=body.url,
            password=body.password,
        )
    except SecretVaultLocked:
        raise _locked() from None
    except NotFoundError:
        raise HTTPException(status_code=404, detail="vault entry not found") from None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return _out(view)


@router.delete("/entries/{entry_id}", status_code=204)
async def delete_entry(entry_id: str, request: Request) -> None:
    try:
        await deps.secret_vault(request).delete(OPERATOR_ID, entry_id)
    except SecretVaultLocked:
        raise _locked() from None
    except NotFoundError:
        raise HTTPException(status_code=404, detail="vault entry not found") from None
