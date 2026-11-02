"""Health: the public liveness probe, and the operator's per-service health readout.

``/health`` is public (see ``core.auth``); ``/health/services`` reads operator data and
stays behind the gate.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel

from core.config import get_settings
from core.health import HealthStatus
from models.registry import ModelEndpoint
from routes import deps
from routes.deps import OPERATOR_ID
from services.capabilities import Capability, build_capabilities

router = APIRouter(tags=["health"])


class Health(BaseModel):
    status: str
    version: str


@router.get("/health", response_model=Health)
async def health() -> Health:
    return Health(status="ok", version=get_settings().version)


class ServiceHealth(Capability):
    """One Health-page row: a workspace capability or an enabled model endpoint."""

    kind: Literal["capability", "endpoint"]
    # When the status was last measured. Endpoints only — capabilities are live reads.
    checked_at: datetime | None = None


def _endpoint_health(endpoint: ModelEndpoint) -> ServiceHealth:
    """A row from the endpoint's last stored connection test; nothing is probed here."""
    status: HealthStatus
    if endpoint.last_status == "ok":
        status, detail = "nominal", endpoint.model or endpoint.base_url
    elif endpoint.last_status == "error":
        status = "alert"
        detail = endpoint.last_error_detail or "last connection test failed"
    else:
        # Never tested is unknown, not a fault.
        status, detail = "off", "not tested yet"
    return ServiceHealth(
        kind="endpoint",
        key=f"endpoint:{endpoint.id}",
        label=endpoint.name.upper(),
        status=status,
        detail=detail,
        remediation_href=None if status == "nominal" else "/models",
        remediation_label=None if status == "nominal" else "CONFIGURE",
        checked_at=endpoint.last_checked_at,
    )


@router.get("/health/services", response_model=list[ServiceHealth])
async def list_service_health(request: Request) -> list[ServiceHealth]:
    capabilities = await build_capabilities(request)
    endpoints = await deps.models(request).list_endpoints(OPERATOR_ID)
    return [
        *(
            ServiceHealth.model_validate({**c.model_dump(), "kind": "capability"})
            for c in capabilities
        ),
        *(_endpoint_health(e) for e in endpoints if e.enabled),
    ]
