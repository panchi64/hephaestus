"""Home overview — a read-only aggregate of real chassis status.

The home page is presentation-only; this endpoint is the single source of truth
it renders: the build version, counts of the operator's conversations, memories,
endpoints and active runs, and the capability health from `services.capabilities`.
Telemetry and external services that don't exist yet are absent rather than fabricated.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request
from pydantic import BaseModel

from core.config import get_settings
from routes import deps
from routes.deps import OPERATOR_ID
from services.capabilities import Capability, build_capabilities

router = APIRouter(prefix="/overview", tags=["overview"])


class Overview(BaseModel):
    version: str
    endpoint_count: int
    conversation_count: int
    memory_count: int
    active_run_count: int
    capabilities: list[Capability]


@router.get("", response_model=Overview)
async def get_overview(request: Request) -> Overview:
    # Independent database reads, each on its own threadpool hop — gathered because this
    # is the home screen's one request.
    endpoints, conversation_count, memory_count, capabilities = await asyncio.gather(
        deps.models(request).list_endpoints(OPERATOR_ID),
        deps.store(request).count_conversations(
            OPERATOR_ID, include_private=deps.private_visible(request)
        ),
        deps.memory(request).count(OPERATOR_ID),
        build_capabilities(request),
    )
    active_runs = [r for r in deps.registry(request).list(OPERATOR_ID) if not r.is_terminal]
    return Overview(
        version=get_settings().version,
        endpoint_count=len(endpoints),
        conversation_count=conversation_count,
        memory_count=memory_count,
        active_run_count=len(active_runs),
        capabilities=capabilities,
    )
