"""API Tokens surface — the operator's outbound service credentials.

Set/clear the API keys the system calls third-party services with. The key is
**write-only**: accepted on set and sealed with the vault, never returned — listings expose
only ``has_key``. The set of services is what the features declare (`known_services`); an
unknown id is a 404.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from core.exceptions import NotFoundError
from routes import deps
from routes.deps import OPERATOR_ID
from services.credential_store import known_services

router = APIRouter(prefix="/credentials", tags=["credentials"])


class CredentialSet(BaseModel):
    api_key: str  # "" clears the stored key


class CredentialView(BaseModel):
    service: str
    label: str
    purpose: str
    docs_url: str
    has_key: bool  # never the key itself


def _view(service: str, has_key: bool) -> CredentialView:
    info = next(s for s in known_services() if s.id == service)
    return CredentialView(
        service=info.id,
        label=info.label,
        purpose=info.purpose,
        docs_url=info.docs_url,
        has_key=has_key,
    )


@router.get("", response_model=list[CredentialView])
async def list_credentials(request: Request) -> list[CredentialView]:
    present = await deps.credentials(request).status(OPERATOR_ID)
    return [_view(s.id, present.get(s.id, False)) for s in known_services()]


@router.put("/{service}", response_model=CredentialView)
async def set_credential(service: str, body: CredentialSet, request: Request) -> CredentialView:
    try:
        await deps.credentials(request).set_key(OPERATOR_ID, service, body.api_key)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="unknown service") from None
    return _view(service, has_key=bool(body.api_key))


@router.delete("/{service}", response_model=CredentialView)
async def clear_credential(service: str, request: Request) -> CredentialView:
    try:
        await deps.credentials(request).clear_key(OPERATOR_ID, service)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="unknown service") from None
    return _view(service, has_key=False)
