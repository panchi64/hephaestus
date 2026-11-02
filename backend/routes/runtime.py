"""Runtime readout — the versions this backend runs on, the model behind each role, and
how many agent tools are on. Read-only; nothing here is probed."""

from __future__ import annotations

import asyncio
import platform
from importlib.metadata import version

from fastapi import APIRouter, Request
from pydantic import BaseModel

from core.config import get_settings
from routes import deps
from routes.deps import OPERATOR_ID
from routes.tools import assembled_catalog
from services.tool_policy import get_disabled_tools

router = APIRouter(prefix="/runtime", tags=["runtime"])

# Display order; `test_runtime_lists_every_role` holds it equal to `llm.ROLES`.
ROLE_ORDER = ("main", "utility", "subagent", "embedding")


class RoleModel(BaseModel):
    role: str
    # Head of the role's chain; both null when the role is unbound.
    endpoint: str | None = None
    model: str | None = None
    # `main` with nothing bound, reporting the backend's default (see `implicit_main_binding`).
    implicit: bool = False


class ToolCounts(BaseModel):
    total: int
    enabled: int


class Runtime(BaseModel):
    version: str
    pydantic_ai: str
    pydantic_ai_harness: str
    python: str
    roles: list[RoleModel]
    tools: ToolCounts


async def _role_models(request: Request) -> list[RoleModel]:
    models = deps.models(request)
    chains, pinned, endpoints = await asyncio.gather(
        models.list_roles(OPERATOR_ID),
        models.list_role_models(OPERATOR_ID),
        models.list_endpoints(OPERATOR_ID),
    )
    by_id = {e.id: e for e in endpoints}
    implicit_main = None if chains.get("main") else await models.implicit_main_binding(OPERATOR_ID)

    out: list[RoleModel] = []
    for role in ROLE_ORDER:
        chain, model, implicit = chains.get(role, []), pinned.get(role), False
        if role == "main" and implicit_main is not None:
            (chain, model), implicit = implicit_main, True
        head = by_id.get(chain[0]) if chain else None
        out.append(
            RoleModel(
                role=role,
                endpoint=head.name if head else None,
                model=model or (head.model if head else None),
                implicit=implicit,
            )
        )
    return out


@router.get("", response_model=Runtime)
async def get_runtime(request: Request) -> Runtime:
    roles, disabled = await asyncio.gather(
        _role_models(request),
        get_disabled_tools(deps.settings_store(request), OPERATOR_ID),
    )
    catalog = assembled_catalog(request)
    return Runtime(
        version=get_settings().version,
        pydantic_ai=version("pydantic-ai"),
        pydantic_ai_harness=version("pydantic-ai-harness"),
        python=platform.python_version(),
        roles=roles,
        tools=ToolCounts(
            total=len(catalog),
            enabled=sum(1 for t in catalog if t.name not in disabled),
        ),
    )
