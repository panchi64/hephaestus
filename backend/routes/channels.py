"""The chats messaging channels continue, and unlinking one."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel

from routes import deps
from routes.deps import OPERATOR_ID
from services.channels import ChannelLinks

router = APIRouter(prefix="/channels", tags=["channels"])


class LinkOut(BaseModel):
    id: str
    channel: str
    address: str
    conversation_id: str
    created_at: datetime


@router.get("/links", response_model=list[LinkOut])
async def list_links(request: Request) -> list[LinkOut]:
    links = await deps.service(request, ChannelLinks).list(OPERATOR_ID)
    return [
        LinkOut(
            id=link.id,
            channel=link.channel,
            address=link.address,
            conversation_id=link.conversation_id,
            created_at=link.created_at,
        )
        for link in links
    ]


@router.delete("/links/{link_id}", status_code=204)
async def unlink(request: Request, link_id: str) -> Response:
    """The address's next message opens a new chat."""
    await deps.service(request, ChannelLinks).remove(OPERATOR_ID, link_id)
    return Response(status_code=204)
