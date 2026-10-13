"""Web search provider surface — manage the operator's search providers.

CRUD over the SearXNG providers the agent's `search` tool queries. The API key is
**write-only**: accepted on create/update and sealed with the vault, but never
returned — listings expose only ``has_api_key``. The agent reaches search through
the tool, not this surface; this is configuration only.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from core.exceptions import NotFoundError
from models.search import SearchProvider
from routes import deps
from routes.deps import OPERATOR_ID

router = APIRouter(prefix="/search", tags=["search"])


class ProviderCreate(BaseModel):
    name: str
    base_url: str
    enabled: bool = True
    engines: list[str] = []
    params: dict = {}
    api_key: str | None = None


class ProviderUpdate(BaseModel):
    name: str | None = None
    base_url: str | None = None
    enabled: bool | None = None
    engines: list[str] | None = None
    params: dict | None = None
    api_key: str | None = None  # "" clears the key; omitted leaves it unchanged


class ProviderView(BaseModel):
    id: str
    name: str
    base_url: str
    enabled: bool
    engines: list[str]
    params: dict
    has_api_key: bool


def _view(provider: SearchProvider) -> ProviderView:
    return ProviderView(
        id=provider.id,
        name=provider.name,
        base_url=provider.base_url,
        enabled=provider.enabled,
        engines=provider.engines,
        params=provider.params,
        has_api_key=provider.api_key_enc is not None,
    )


@router.get("/providers", response_model=list[ProviderView])
async def list_providers(request: Request) -> list[ProviderView]:
    providers = await deps.search(request).list_providers(OPERATOR_ID)
    return [_view(p) for p in providers]


@router.post("/providers", status_code=201, response_model=ProviderView)
async def create_provider(body: ProviderCreate, request: Request) -> ProviderView:
    provider = await deps.search(request).create_provider(OPERATOR_ID, **body.model_dump())
    return _view(provider)


@router.get("/providers/{provider_id}", response_model=ProviderView)
async def get_provider(provider_id: str, request: Request) -> ProviderView:
    try:
        provider = await deps.search(request).get_provider(OPERATOR_ID, provider_id)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="search provider not found") from None
    return _view(provider)


@router.patch("/providers/{provider_id}", response_model=ProviderView)
async def update_provider(provider_id: str, body: ProviderUpdate, request: Request) -> ProviderView:
    changes = body.model_dump(exclude_unset=True)
    try:
        provider = await deps.search(request).update_provider(OPERATOR_ID, provider_id, **changes)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="search provider not found") from None
    return _view(provider)


@router.delete("/providers/{provider_id}", status_code=204)
async def delete_provider(provider_id: str, request: Request) -> None:
    try:
        await deps.search(request).delete_provider(OPERATOR_ID, provider_id)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="search provider not found") from None
