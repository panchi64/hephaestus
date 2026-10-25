"""The first-run walkthrough: its completion flag, the local-server scan, and the check that
the workspace can hold a conversation before the walkthrough lets the operator in."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from routes import deps
from routes.deps import OPERATOR_ID
from services import onboarding

router = APIRouter(prefix="/onboarding", tags=["onboarding"])


class OnboardingState(BaseModel):
    completed: bool


class LocalModelView(BaseModel):
    id: str
    context_window: int | None
    parallel_slots: int | None
    loaded: bool | None
    kind: str | None


class LocalServerView(BaseModel):
    label: str
    base_url: str
    models: list[LocalModelView]


@router.get("", response_model=OnboardingState)
async def get_onboarding(request: Request) -> OnboardingState:
    return OnboardingState(
        completed=await onboarding.is_completed(deps.settings_store(request), OPERATOR_ID)
    )


@router.get("/local-endpoints", response_model=list[LocalServerView])
async def local_endpoints(request: Request) -> list[LocalServerView]:
    servers = await onboarding.detect_local(deps.models(request).http_client)
    return [
        LocalServerView(
            label=server.label,
            base_url=server.base_url,
            models=[
                LocalModelView(
                    id=m.id,
                    context_window=m.context_window,
                    parallel_slots=m.parallel_slots,
                    loaded=m.loaded,
                    kind=m.kind,
                )
                for m in server.models
            ],
        )
        for server in servers
    ]


@router.post("/complete", response_model=OnboardingState)
async def complete(request: Request) -> OnboardingState:
    """Refuses (409) while the workspace still can't hold a conversation, naming the step."""
    registry = deps.models(request)
    endpoints = await registry.list_endpoints(OPERATOR_ID)
    if not any(e.enabled and e.last_status == "ok" for e in endpoints):
        raise HTTPException(status_code=409, detail="No endpoint has passed its connection test.")
    chain, _ = await registry.get_role_binding(OPERATOR_ID, "main")
    if not chain:
        raise HTTPException(status_code=409, detail="No chat model is picked.")
    working = {e.id for e in endpoints if e.enabled and e.last_status == "ok"}
    if not working.intersection(chain):
        raise HTTPException(
            status_code=409,
            detail="The chat model's endpoint hasn't passed its connection test.",
        )
    if await registry.role_context_window(OPERATOR_ID, "main") is None:
        raise HTTPException(
            status_code=409, detail="The chat model's endpoint reports no context window."
        )
    await onboarding.mark_completed(deps.settings_store(request), OPERATOR_ID)
    return OnboardingState(completed=True)


@router.post("/reopen", response_model=OnboardingState)
async def reopen(request: Request) -> OnboardingState:
    await onboarding.mark_pending(deps.settings_store(request), OPERATOR_ID)
    return OnboardingState(completed=False)
