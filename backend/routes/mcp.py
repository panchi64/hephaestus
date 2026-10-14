"""External MCP tool servers (`MCP-1`…`MCP-3`).

Thin pass-throughs to :class:`~services.mcp.registry.McpRegistry`: register a server,
list what each exposes, reconnect one, disable or remove it, and set the per-tool
enable/trust decisions.

Two things this surface is deliberate about:

* **A failed connection is a 200 with a status, not an error.** ``POST /connect`` always
  answers with the server's current view; an unreachable server comes back as
  ``status: "error"`` carrying the reason. The operator asked *whether* it connects, and
  a 502 would tell them less than the message does.
* **Trust is set one tool at a time.** There is deliberately no server-level "trust
  everything" endpoint — an external tool's effects aren't knowable to the system
  (`AE-3.6`), so blanket-approving a server's whole catalog is exactly the decision the
  operator must not be able to make by accident.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from core.exceptions import DegradedCapabilityError
from routes import deps
from routes.camel import CamelModel
from routes.deps import OPERATOR_ID
from services.mcp import McpServerView

router = APIRouter(prefix="/mcp", tags=["mcp"])


class McpServerCreate(BaseModel):
    name: str
    transport: str
    # stdio
    command: str | None = None
    args: list[str] = []
    env: dict[str, str] | None = None
    # sse / http
    url: str | None = None
    auth_required: bool = False
    credentials: dict[str, object] | None = None


class McpServerUpdate(BaseModel):
    name: str | None = None
    enabled: bool | None = None
    command: str | None = None
    args: list[str] | None = None
    env: dict[str, str] | None = None
    url: str | None = None
    auth_required: bool | None = None
    credentials: dict[str, object] | None = None


class McpToolPolicyUpdate(BaseModel):
    enabled: bool | None = None
    trusted: bool | None = None


class McpToolOut(CamelModel):
    name: str
    description: str
    enabled: bool
    trusted: bool


class McpServerOut(CamelModel):
    id: str
    name: str
    slug: str
    transport: str
    url: str | None
    command: str | None
    args: list[str]
    env_keys: list[str]
    enabled: bool
    status: str
    auth_required: bool
    has_credentials: bool
    last_error: str | None
    last_error_at: datetime | None
    tools: list[McpToolOut]
    created_at: datetime
    updated_at: datetime


def _out(view: McpServerView) -> McpServerOut:
    return McpServerOut(
        id=view.id,
        name=view.name,
        slug=view.slug,
        transport=view.transport,
        url=view.url,
        command=view.command,
        args=view.args,
        env_keys=view.env_keys,
        enabled=view.enabled,
        status=view.status,
        auth_required=view.auth_required,
        has_credentials=view.has_credentials,
        last_error=view.last_error,
        last_error_at=view.last_error_at,
        tools=[
            McpToolOut(name=t.name, description=t.description, enabled=t.enabled, trusted=t.trusted)
            for t in view.tools
        ],
        created_at=view.created_at,
        updated_at=view.updated_at,
    )


@router.get("/servers")
async def list_servers(request: Request) -> list[McpServerOut]:
    return [_out(v) for v in await deps.mcp(request).list(OPERATOR_ID)]


@router.post("/servers", status_code=201)
async def register_server(request: Request, body: McpServerCreate) -> McpServerOut:
    try:
        view = await deps.mcp(request).register(
            OPERATOR_ID,
            name=body.name,
            transport=body.transport,
            command=body.command,
            args=body.args,
            env=body.env,
            url=body.url,
            auth_required=body.auth_required,
            credentials=body.credentials,
        )
    except DegradedCapabilityError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _out(view)


@router.patch("/servers/{server_id}")
async def update_server(request: Request, server_id: str, body: McpServerUpdate) -> McpServerOut:
    view = await deps.mcp(request).update(
        OPERATOR_ID,
        server_id,
        name=body.name,
        enabled=body.enabled,
        command=body.command,
        args=body.args,
        env=body.env,
        url=body.url,
        auth_required=body.auth_required,
        credentials=body.credentials,
    )
    return _out(view)


@router.delete("/servers/{server_id}", status_code=204)
async def remove_server(request: Request, server_id: str) -> Response:
    await deps.mcp(request).remove(OPERATOR_ID, server_id)
    return Response(status_code=204)


@router.post("/servers/{server_id}/connect")
async def connect_server(request: Request, server_id: str) -> McpServerOut:
    """Reconnect and re-discover (`MCP-3`). Answers with the outcome either way — a
    server that refuses comes back with ``status: "error"`` and the reason."""
    view = await deps.mcp(request).connect(OPERATOR_ID, server_id)
    return _out(view)


@router.patch("/servers/{server_id}/tools/{tool_name}")
async def set_tool_policy(
    request: Request, server_id: str, tool_name: str, body: McpToolPolicyUpdate
) -> McpToolOut:
    """Enable/disable a single tool (`MCP-1`) or mark it trusted / revoke that trust
    (`AE-3.6`). Marking trusted is an operator action; the agent never reaches here."""
    registry = deps.mcp(request)
    policy = await registry.set_tool_policy(
        OPERATOR_ID, server_id, tool_name, enabled=body.enabled, trusted=body.trusted
    )
    view = await registry.get(OPERATOR_ID, server_id)
    description = next((t.description for t in view.tools if t.name == tool_name), "")
    return McpToolOut(
        name=tool_name,
        description=description,
        enabled=policy.enabled,
        trusted=policy.trusted,
    )
