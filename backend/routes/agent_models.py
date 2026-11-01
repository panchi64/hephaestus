"""The models Talos may choose from when it spawns an agent with no pin of its own.

Under ``/models`` beside the role bindings, because it is the same kind of setting: which
model does what. Talos reads the list through ``agents_models`` and nothing else.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from core.exceptions import DegradedCapabilityError, NotFoundError
from routes import deps
from routes.deps import OPERATOR_ID
from services.agent_models import ChoosableModel
from services.subagents.models import offer

router = APIRouter(prefix="/models", tags=["models"])


class ChoosableView(BaseModel):
    endpoint_id: str
    model: str
    note: str
    #: The id Talos passes as ``model``; empty while unavailable.
    id: str
    #: False when the endpoint is gone, disabled or can't drive tools: kept, not offered.
    available: bool
    local: bool


class ChoosableList(BaseModel):
    models: list[ChoosableModel]


async def _views(request: Request, entries: list[ChoosableModel]) -> list[ChoosableView]:
    registry = deps.models(request)
    offers = await asyncio.gather(*(offer(registry, entry, OPERATOR_ID) for entry in entries))
    return [
        ChoosableView(
            endpoint_id=entry.endpoint_id,
            model=entry.model,
            note=entry.note,
            id=found.id if found is not None else "",
            available=found is not None,
            local=found.local if found is not None else False,
        )
        for entry, found in zip(entries, offers, strict=True)
    ]


@router.get("/choosable", response_model=list[ChoosableView])
async def list_choosable(request: Request) -> list[ChoosableView]:
    return await _views(request, await deps.agent_models(request).choosable(OPERATOR_ID))


@router.put("/choosable", response_model=list[ChoosableView])
async def set_choosable(body: ChoosableList, request: Request) -> list[ChoosableView]:
    """Replace the list. A new entry must name a tool-calling endpoint that can serve it;
    one already saved stays even while its endpoint is unavailable, so an edit elsewhere in
    the list still saves."""
    settings = deps.agent_models(request)
    saved = {(e.endpoint_id, e.model) for e in await settings.choosable(OPERATOR_ID)}
    registry = deps.models(request)
    for entry in body.models:
        if (entry.endpoint_id, entry.model) in saved:
            continue
        try:
            _ = await registry.resolve_specs(
                "main",
                owner_id=OPERATOR_ID,
                override_endpoint_id=entry.endpoint_id,
                override_model=entry.model,
            )
        except NotFoundError:
            raise HTTPException(status_code=404, detail="endpoint not found") from None
        except DegradedCapabilityError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    await settings.set_choosable(OPERATOR_ID, body.models)
    return await _views(request, await settings.choosable(OPERATOR_ID))
