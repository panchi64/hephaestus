"""Scoped API tokens (`AUTH-4`) — issue, list, revoke.

**Inbound** auth: tokens issued to clients for programmatic access, scoped and revocable.
Deliberately separate from ``routes/api_tokens.py`` (prefix ``/credentials``), which holds
the **outbound** third-party service keys the system calls other services with.

The plaintext token is in the issue response and nowhere else — the store keeps only a
one-way hash, so a lost token is reissued rather than recovered. This surface is itself
outside every scope (`core.api_scopes`), so a token can never mint or revoke another.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from core.exceptions import NotFoundError
from routes import deps
from routes.deps import OPERATOR_ID
from services.api_token_store import ApiTokenInfo

router = APIRouter(prefix="/tokens", tags=["tokens"])


class ScopeView(BaseModel):
    id: str
    label: str
    description: str


class TokenView(BaseModel):
    id: str
    label: str
    # The public half of the token, so the operator can match a row to a client.
    prefix: str
    scopes: list[str]
    created_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None


class IssuedTokenView(TokenView):
    # The only time the plaintext is ever returned.
    token: str


class TokenCreate(BaseModel):
    label: str
    scopes: list[str]


def _view(info: ApiTokenInfo) -> TokenView:
    return TokenView(
        id=info.id,
        label=info.label,
        prefix=info.prefix,
        scopes=info.scopes,
        created_at=info.created_at,
        last_used_at=info.last_used_at,
        revoked_at=info.revoked_at,
    )


@router.get("/scopes", response_model=list[ScopeView])
async def list_scopes(request: Request) -> list[ScopeView]:
    """The catalog a token's scopes are chosen from — the app's assembled table, so
    a scope is only offered when something actually claims a surface into it."""
    return [
        ScopeView(id=scope.id, label=scope.label, description=scope.description)
        for scope in deps.api_scope_table(request).scopes
    ]


@router.get("", response_model=list[TokenView])
async def list_tokens(request: Request) -> list[TokenView]:
    return [_view(info) for info in await deps.api_tokens(request).list(OPERATOR_ID)]


@router.post("", response_model=IssuedTokenView, status_code=201)
async def issue_token(body: TokenCreate, request: Request) -> IssuedTokenView:
    try:
        issued = await deps.api_tokens(request).issue(OPERATOR_ID, body.label, body.scopes)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    view = _view(issued.info)
    return IssuedTokenView(
        id=view.id,
        label=view.label,
        prefix=view.prefix,
        scopes=view.scopes,
        created_at=view.created_at,
        last_used_at=view.last_used_at,
        revoked_at=view.revoked_at,
        token=issued.token,
    )


@router.delete("/{token_id}", response_model=TokenView)
async def revoke_token(token_id: str, request: Request) -> TokenView:
    try:
        return _view(await deps.api_tokens(request).revoke(OPERATOR_ID, token_id))
    except NotFoundError:
        raise HTTPException(status_code=404, detail="unknown token") from None
