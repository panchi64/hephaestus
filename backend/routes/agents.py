"""Agents surface — every agent the operator can run, and the ones they write.

Built-in and project agents are listed read-only beside the stored ones, each with an id:
a stored agent's row id, or ``builtin:<name>`` / ``project:<name>``. Publishing is its own
endpoint because it is the boundary between a draft and instructions the model acts under.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, File, HTTPException, Request, Response, UploadFile
from pydantic import BaseModel

from core.exceptions import AgentValidationError, NotFoundError
from routes import deps
from routes.deps import OPERATOR_ID
from routes.revisions import add_revision_routes
from services.agents import BUILTIN, AgentDefinition, ToolAccess, project_agents
from services.agents.definition import AgentSource, WorkspacePolicy
from services.agents.files import render_agent_file
from services.permissions import PermissionLevel

router = APIRouter(prefix="/agents", tags=["agents"])
add_revision_routes(router, deps.agent_store)

_IMPORT_MAX_BYTES = 64 * 1024

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
    catalog = deps.agents(request)
    contributed = await catalog.contributed(OPERATOR_ID)
    effective = await catalog.agents(OPERATOR_ID, project_root=root)
    return [
        _out(agent, active=effective.get(agent.name) == agent)
        for agent in (*BUILTIN, *declared, *contributed, *stored)
    ]


@router.get("/{agent_id}", response_model=AgentOut)
async def get_agent(request: Request, agent_id: str, project_id: str | None = None) -> AgentOut:
    root = await deps.composer_root(request, project_id, None)
    effective = await deps.agents(request).agents(OPERATOR_ID, project_root=root)
    agent = await _find(request, agent_id, root)
    return _out(agent, active=effective.get(agent.name) == agent)


class AgentDocument(BaseModel):
    """An agent as its whole file: frontmatter and instructions."""

    content: str


@router.get("/{agent_id}/document", response_model=AgentDocument)
async def get_agent_document(
    request: Request, agent_id: str, project_id: str | None = None
) -> AgentDocument:
    root = await deps.composer_root(request, project_id, None)
    return AgentDocument(content=render_agent_file(await _find(request, agent_id, root)))


@router.put("/{agent_id}/document", response_model=AgentOut)
async def put_agent_document(request: Request, agent_id: str, body: AgentDocument) -> AgentOut:
    """Rewrite a stored agent from its edited file, validated as an import would be."""
    try:
        agent = await deps.agent_store(request).replace_document(
            OPERATOR_ID, agent_id, body.content
        )
    except AgentValidationError as exc:
        raise _invalid(exc) from None
    return await _resolved(request, agent)


@router.get("/{agent_id}/export")
async def export_agent(request: Request, agent_id: str, project_id: str | None = None) -> Response:
    """The agent as a `.claude/agents` file, which a project or another installation reads."""
    root = await deps.composer_root(request, project_id, None)
    agent = await _find(request, agent_id, root)
    return Response(
        content=render_agent_file(agent),
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{agent.name}.md"'},
    )


@router.post("/import", response_model=AgentOut, status_code=201)
async def import_agent(request: Request, file: Annotated[UploadFile, File()]) -> AgentOut:
    """Import an agent file as a draft."""
    content = await file.read(_IMPORT_MAX_BYTES + 1)
    if not content:
        raise HTTPException(status_code=422, detail="file is empty")
    if len(content) > _IMPORT_MAX_BYTES:
        raise HTTPException(status_code=413, detail="an agent file is at most 64 KB")
    try:
        text = content.decode("utf-8")
        agent = await deps.agent_store(request).import_file(
            OPERATOR_ID, text, filename=file.filename or "agent.md"
        )
    except UnicodeDecodeError:
        raise HTTPException(status_code=422, detail="an agent file is UTF-8 text") from None
    except AgentValidationError as exc:
        raise _invalid(exc) from None
    return await _resolved(request, agent)


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


async def _find(request: Request, agent_id: str, root: Path | None) -> AgentDefinition:
    source, _, name = agent_id.partition(":")
    if source == "builtin":
        agent = next((a for a in BUILTIN if a.name == name), None)
    elif source == "project":
        agent = next((a for a in project_agents(root) if a.name == name), None) if root else None
    elif source == "plugin":
        contributed = await deps.agents(request).contributed(OPERATOR_ID)
        agent = next((a for a in contributed if a.name == name), None)
    else:
        agent = await deps.agent_store(request).get(OPERATOR_ID, agent_id)
    if agent is None:
        raise NotFoundError("agent not found")
    return agent


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
