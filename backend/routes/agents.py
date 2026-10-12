"""Agents surface — every agent the operator can run, and the ones they write.

Built-in and project agents are listed read-only beside the stored ones, each with an id:
a stored agent's row id, or ``builtin:<name>`` / ``project:<name>``. Publishing is its own
endpoint because it is the boundary between a draft and instructions the model acts under.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from core.exceptions import AgentValidationError, NotFoundError
from routes import deps
from routes.deps import OPERATOR_ID
from services.agents import BUILTIN, AgentDefinition, ToolAccess, project_agents
from services.agents.definition import AgentSource, WorkspacePolicy
from services.permissions import PermissionLevel

router = APIRouter(prefix="/agents", tags=["agents"])

_READ_ONLY_SOURCES: frozenset[str] = frozenset({"builtin", "project", "plugin"})


class AgentOut(BaseModel):
    id: str
    name: str
    description: str
    instructions: str
    endpoint_id: str | None
    model: str | None
    #: Null offers every tool.
    tools: list[str] | None
    deny_tools: list[str]
    permission_ceiling: str | None
    workspace: WorkspacePolicy
    request_limit: int | None
    wall_clock_timeout_s: float | None
    can_delegate: bool
    max_depth: int | None
    #: Whether other agents may delegate to it.
    delegable: bool
    source: AgentSource
    published: bool
    #: Whether its name resolves to this definition rather than to one that shadows it.
    active: bool
    editable: bool


class AgentWrite(BaseModel):
    name: str | None = None
    description: str | None = None
    instructions: str | None = None
    endpoint_id: str | None = None
    model: str | None = None
    tools: list[str] | None = None
    deny_tools: list[str] | None = None
    permission_ceiling: PermissionLevel | None = None
    workspace: WorkspacePolicy | None = None
    request_limit: int | None = None
    wall_clock_timeout_s: float | None = None
    can_delegate: bool | None = None
    max_depth: int | None = None


@router.get("", response_model=list[AgentOut])
async def list_agents(request: Request, project_id: str | None = None) -> list[AgentOut]:
    """Every agent: built-ins, the project's (when ``project_id`` names one), and stored
    agents including drafts."""
    root = await deps.composer_root(request, project_id, None)
    stored = await deps.agent_store(request).list(OPERATOR_ID)
    declared = project_agents(root) if root is not None else ()
    effective = await deps.agents(request).agents(OPERATOR_ID, project_root=root)
    return [
        _out(agent, active=effective.get(agent.name) == agent)
        for agent in (*BUILTIN, *declared, *stored)
    ]


@router.get("/{agent_id}", response_model=AgentOut)
async def get_agent(request: Request, agent_id: str, project_id: str | None = None) -> AgentOut:
    root = await deps.composer_root(request, project_id, None)
    effective = await deps.agents(request).agents(OPERATOR_ID, project_root=root)
    source, _, name = agent_id.partition(":")
    if source == "builtin":
        agent = next((a for a in BUILTIN if a.name == name), None)
    elif source == "project":
        agent = next((a for a in project_agents(root) if a.name == name), None) if root else None
    else:
        agent = await deps.agent_store(request).get(OPERATOR_ID, agent_id)
    if agent is None:
        raise NotFoundError("agent not found")
    return _out(agent, active=effective.get(agent.name) == agent)


@router.post("", response_model=AgentOut, status_code=201)
async def create_agent(request: Request, body: AgentWrite) -> AgentOut:
    try:
        agent = await deps.agent_store(request).create(OPERATOR_ID, _values(body))
    except AgentValidationError as exc:
        raise _invalid(exc) from None
    return await _resolved(request, agent)


@router.patch("/{agent_id}", response_model=AgentOut)
async def update_agent(request: Request, agent_id: str, body: AgentWrite) -> AgentOut:
    store = deps.agent_store(request)
    current = await store.get(OPERATOR_ID, agent_id)
    try:
        agent = await store.update(OPERATOR_ID, agent_id, _values(body, current))
    except AgentValidationError as exc:
        raise _invalid(exc) from None
    return await _resolved(request, agent)


@router.post("/{agent_id}/publish", response_model=AgentOut)
async def publish_agent(request: Request, agent_id: str) -> AgentOut:
    agent = await deps.agent_store(request).set_published(OPERATOR_ID, agent_id, True)
    return await _resolved(request, agent)


@router.post("/{agent_id}/unpublish", response_model=AgentOut)
async def unpublish_agent(request: Request, agent_id: str) -> AgentOut:
    agent = await deps.agent_store(request).set_published(OPERATOR_ID, agent_id, False)
    return await _resolved(request, agent)


@router.delete("/{agent_id}", status_code=204)
async def delete_agent(request: Request, agent_id: str) -> None:
    await deps.agent_store(request).delete(OPERATOR_ID, agent_id)


async def _resolved(request: Request, agent: AgentDefinition) -> AgentOut:
    effective = await deps.agents(request).agents(OPERATOR_ID)
    return _out(agent, active=effective.get(agent.name) == agent)


def _values(body: AgentWrite, current: AgentDefinition | None = None) -> dict[str, Any]:
    """The fields the body sets, as the store's values. An explicit null clears a field."""
    sent = body.model_dump(exclude_unset=True)
    tools = sent.pop("tools", ...)
    deny = sent.pop("deny_tools", ...)
    if tools is not ... or deny is not ...:
        base = current.tools if current is not None else ToolAccess()
        sent["tools"] = ToolAccess(
            allow=base.allow if tools is ... else (None if tools is None else frozenset(tools)),
            deny=base.deny if deny is ... else frozenset(deny or ()),
        )
    return sent


def _out(agent: AgentDefinition, *, active: bool) -> AgentOut:
    return AgentOut(
        id=agent.id or f"{agent.source}:{agent.name}",
        name=agent.name,
        description=agent.description,
        instructions=agent.instructions,
        endpoint_id=agent.endpoint_id,
        model=agent.model,
        tools=None if agent.tools.allow is None else sorted(agent.tools.allow),
        deny_tools=sorted(agent.tools.deny),
        permission_ceiling=agent.permission_ceiling,
        workspace=agent.workspace,
        request_limit=agent.request_limit,
        wall_clock_timeout_s=agent.wall_clock_timeout_s,
        can_delegate=agent.can_delegate,
        max_depth=agent.max_depth,
        delegable=agent.delegable,
        source=agent.source,
        published=agent.published,
        active=active,
        editable=agent.source not in _READ_ONLY_SOURCES,
    )


def _invalid(exc: AgentValidationError) -> HTTPException:
    return HTTPException(status_code=422, detail={"field": exc.field, "message": str(exc)})
