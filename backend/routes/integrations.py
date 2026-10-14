"""Third-party connectors (`INTEG-1`…`INTEG-3`).

Thin pass-throughs to :class:`~services.integrations.service.IntegrationService`: read the
preset catalog, configure a connector from one, test its credentials, amend or remove it,
and set the per-action enable/trust decisions.

Owned by the external-tools track alongside ``routes/mcp.py`` because both ride the one
`AE-3.6` per-tool trust mechanism — a connector's action and an MCP server's tool are the
same kind of unknown, and the operator grants trust to each the same way.

Two things this surface is deliberate about:

* **Testing is its own endpoint, not a side effect of saving.** `INTEG-3` is "prove the
  credential *before* relying on it", which is a decision the operator makes — so
  configuring stores, and testing proves. A failed test answers 200 with
  ``status: "error"`` and the reason, exactly like an MCP connect.
* **The credential goes in and never comes back.** Bodies accept ``credentials``; no
  response shape can return one. The surface reports ``configured``, not the secret.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from core.exceptions import DegradedCapabilityError, SSRFError
from routes import deps
from routes.camel import CamelModel
from routes.deps import OPERATOR_ID
from services.integrations import IntegrationActionView, IntegrationService, IntegrationView

router = APIRouter(prefix="/integrations", tags=["integrations"])


class IntegrationCreate(BaseModel):
    preset: str
    name: str | None = None
    base_url: str | None = None
    credentials: dict[str, object] | None = None


class IntegrationUpdate(BaseModel):
    name: str | None = None
    base_url: str | None = None
    credentials: dict[str, object] | None = None
    enabled: bool | None = None


class IntegrationActionPolicyUpdate(BaseModel):
    enabled: bool | None = None
    trusted: bool | None = None


class IntegrationActionOut(CamelModel):
    name: str
    method: str
    path: str
    description: str
    takes_body: bool
    enabled: bool
    trusted: bool


class IntegrationPresetOut(CamelModel):
    id: str
    name: str
    category: str
    description: str
    base_url: str
    auth: str
    credential_required: bool
    actions: list[str]


class IntegrationOut(CamelModel):
    id: str
    name: str
    slug: str
    preset: str
    category: str
    description: str
    base_url: str
    credential_required: bool
    configured: bool
    enabled: bool
    status: str
    last_error: str | None
    last_tested_at: datetime | None
    actions: list[IntegrationActionOut]
    created_at: datetime
    updated_at: datetime


def _action_out(action: IntegrationActionView) -> IntegrationActionOut:
    return IntegrationActionOut(
        name=action.name,
        method=action.method,
        path=action.path,
        description=action.description,
        takes_body=action.takes_body,
        enabled=action.enabled,
        trusted=action.trusted,
    )


def _out(view: IntegrationView) -> IntegrationOut:
    return IntegrationOut(
        id=view.id,
        name=view.name,
        slug=view.slug,
        preset=view.preset,
        category=view.category,
        description=view.description,
        base_url=view.base_url,
        credential_required=view.credential_required,
        configured=view.configured,
        enabled=view.enabled,
        status=view.status,
        last_error=view.last_error,
        last_tested_at=view.last_tested_at,
        actions=[_action_out(a) for a in view.actions],
        created_at=view.created_at,
        updated_at=view.updated_at,
    )


@router.get("/presets")
async def list_presets() -> list[IntegrationPresetOut]:
    """The connectors the operator can configure (`INTEG-1`)."""
    return [
        IntegrationPresetOut(
            id=p.id,
            name=p.name,
            category=p.category,
            description=p.description,
            base_url=p.base_url,
            auth=p.auth,
            credential_required=p.credential_required,
            actions=[a.name for a in p.actions],
        )
        for p in IntegrationService.presets()
    ]


@router.get("")
async def list_integrations(request: Request) -> list[IntegrationOut]:
    return [_out(v) for v in await deps.integrations(request).list(OPERATOR_ID)]


@router.post("", status_code=201)
async def configure_integration(request: Request, body: IntegrationCreate) -> IntegrationOut:
    try:
        view = await deps.integrations(request).configure(
            OPERATOR_ID,
            body.preset,
            name=body.name,
            base_url=body.base_url,
            credentials=body.credentials,
        )
    except DegradedCapabilityError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _out(view)


@router.patch("/{integration_id}")
async def update_integration(
    request: Request, integration_id: str, body: IntegrationUpdate
) -> IntegrationOut:
    view = await deps.integrations(request).update(
        OPERATOR_ID,
        integration_id,
        name=body.name,
        base_url=body.base_url,
        credentials=body.credentials,
        enabled=body.enabled,
    )
    return _out(view)


@router.delete("/{integration_id}", status_code=204)
async def remove_integration(request: Request, integration_id: str) -> Response:
    await deps.integrations(request).remove(OPERATOR_ID, integration_id)
    return Response(status_code=204)


@router.post("/{integration_id}/test")
async def test_integration(request: Request, integration_id: str) -> IntegrationOut:
    """Prove the credential before relying on the connector (`INTEG-3`). Answers with the
    outcome either way — a rejected credential comes back as ``status: "error"``."""
    try:
        view = await deps.integrations(request).test(OPERATOR_ID, integration_id)
    except SSRFError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _out(view)


@router.patch("/{integration_id}/actions/{action_name}")
async def set_action_policy(
    request: Request,
    integration_id: str,
    action_name: str,
    body: IntegrationActionPolicyUpdate,
) -> IntegrationActionOut:
    """Enable/disable one action, or mark it trusted / revoke that trust (`AE-3.6`)."""
    service = deps.integrations(request)
    _ = await service.set_action_policy(
        OPERATOR_ID, integration_id, action_name, enabled=body.enabled, trusted=body.trusted
    )
    view = await service.get(OPERATOR_ID, integration_id)
    return _action_out(next(a for a in view.actions if a.name == action_name))
