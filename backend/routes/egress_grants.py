"""The operator allowing a domain from a command card, for the thread that was refused it.

Mounted under ``/operator``, a prefix no API-token scope claims (``core/api_scopes.py``),
so only the operator's own session reaches it: letting the agent's refused host through is
the operator's decision, and a token holder is not the operator.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from routes import deps
from routes.deps import OPERATOR_ID
from services.workspace import scratch_key

router = APIRouter(prefix="/operator/conversations", tags=["egress"])


class AllowDomain(BaseModel):
    domain: str = Field(min_length=1)


class AllowedDomains(BaseModel):
    #: Every domain the thread's machine may now reach.
    allowed: list[str]


@router.post("/{conversation_id}/egress", response_model=AllowedDomains)
async def allow_domain(conversation_id: str, body: AllowDomain, request: Request) -> AllowedDomains:
    """Let this thread's machine reach ``domain``. A sub-agent's thread grants the
    scratch it shares with the thread that launched it, which is where its commands run."""
    store = deps.store(request)
    if not await store.exists(conversation_id, OPERATOR_ID):
        raise HTTPException(status_code=404, detail="conversation not found")
    binding = await store.binding(conversation_id)
    key = scratch_key(binding.workspace_key or conversation_id)
    allowed = await deps.egress(request).allow(key, [body.domain])
    return AllowedDomains(allowed=sorted(allowed))
