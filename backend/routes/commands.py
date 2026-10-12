"""The command catalog, and the workflows the operator writes into it.

Two surfaces on one router, because they are two views of the same noun. ``GET /commands``
is what a composer reads — thin over :class:`~services.commands.registry.CommandRegistry`,
and everything it appears to decide was decided below this layer: which commands exist,
which of them this thread can actually honour, which one wins a shared name, and what the
headings are called. ``/commands/workflows`` is the editor for the one source the registry
owns, and touches nothing the other four provide.

**The groups ride the catalog response.** A client that kept its own heading list would show
a blank section the first time a source was added below it, so the labels come down with the
rows.

**Validation errors name their field**, matching ``skills``: the message reaches the editor
as a 422 whose detail it renders verbatim, because the backend decides what is valid and the
frontend only shows it. camelCase out, the same way.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from core.exceptions import CommandValidationError, NotFoundError
from routes import deps
from routes.camel import CamelModel
from routes.deps import OPERATOR_ID
from services.commands import GROUPS, CatalogEntry
from services.commands.store import CommandView

router = APIRouter(prefix="/commands", tags=["commands"])


class CommandGroupOut(CamelModel):
    id: str
    label: str
    order: int


class ActionArgumentOut(CamelModel):
    required: bool
    #: A closed set the client may render as a choice, or empty for free text.
    choices: list[str]


class CommandOut(CamelModel):
    name: str
    #: The name that resolves to *this* command and no other (``skill:reviewer``). Always
    #: present, so a client wanting to be unambiguous never has to build one.
    qualified_name: str
    group: str
    title: str
    description: str
    argument_hint: str | None
    kind: str
    #: Set for ``kind == "action"``: the id of a relay the client already owns. One it does
    #: not recognise is a control it should render disabled rather than guess at.
    action_id: str | None
    action_argument: ActionArgumentOut | None
    #: The qualified name that wins this command's bare name, when another source outranks
    #: it. Both rows still ship — the operator gets to see that two things share a name.
    shadowed_by: str | None


class CommandsOut(CamelModel):
    groups: list[CommandGroupOut]
    commands: list[CommandOut]


class WorkflowOut(CamelModel):
    """One saved workflow as its author sees it — including the body, which the catalog
    never carries. The editor is the one place the template is meant to be read."""

    id: str
    name: str
    title: str
    description: str
    body: str
    argument_hint: str | None
    enabled: bool
    created_at: datetime
    updated_at: datetime


class WorkflowCreate(BaseModel):
    name: str
    body: str
    title: str = ""
    description: str = ""
    argument_hint: str = ""
    enabled: bool = True


class WorkflowUpdate(BaseModel):
    """Every field optional, and an omitted one is left unchanged — so the pane can save a
    rename without resending the template."""

    name: str | None = None
    body: str | None = None
    title: str | None = None
    description: str | None = None
    argument_hint: str | None = None
    enabled: bool | None = None


def _out(entry: CatalogEntry) -> CommandOut:
    spec = entry.spec
    argument = (
        ActionArgumentOut(
            required=spec.argument_required, choices=list(spec.action_choices)
        )
        if spec.kind == "action" and (spec.argument_required or spec.action_choices)
        else None
    )
    return CommandOut(
        name=spec.name,
        qualified_name=spec.qualified_name,
        group=spec.source,
        title=spec.title,
        description=spec.description,
        argument_hint=spec.argument_hint,
        kind=spec.kind,
        action_id=spec.action,
        action_argument=argument,
        shadowed_by=entry.shadowed_by,
    )


@router.get("", response_model=CommandsOut)
async def list_commands(
    request: Request,
    conversation_id: str | None = None,
    project_id: str | None = None,
) -> CommandsOut:
    """The commands offerable in a composer, with or without a thread.

    ``conversation_id`` is presence *and* identity. A composer that has not started a
    thread yet cannot compact or fork one, so those rows are simply absent rather than
    shipped and then refused. It also says which tree the project's declarations are read
    from (a worktree thread works on a branch cut from the project) and where the thread
    works. Before a thread exists, ``project_id`` is the only thing that knows which
    project is about to be picked, and a project chat starts in the project's folder.
    """
    if conversation_id is not None:
        workspace_kind = (await deps.store(request).binding(conversation_id)).workspace_kind
    else:
        workspace_kind = "checkout" if project_id else "sandbox"
    entries = await deps.commands(request).catalog(
        OPERATOR_ID,
        workspace_kind=workspace_kind,
        has_conversation=conversation_id is not None,
        disabled_tools=await deps.disabled_tools(request),
        root=await deps.composer_root(request, project_id, conversation_id),
    )
    offered = {entry.spec.source for entry in entries}
    return CommandsOut(
        # Only the headings that have something under them: an empty group is a promise
        # the operator has to click to discover is empty.
        groups=[
            CommandGroupOut(id=group, label=label, order=order)
            for order, (group, label) in enumerate(GROUPS)
            if group in offered
        ],
        commands=[_out(entry) for entry in entries],
    )


# ── workflows: the one source this feature owns ──────────────────────────────────────────


def _workflow_out(view: CommandView) -> WorkflowOut:
    return WorkflowOut(
        id=view.id,
        name=view.name,
        title=view.title,
        description=view.description,
        body=view.body,
        argument_hint=view.argument_hint,
        enabled=view.enabled,
        created_at=view.created_at,
        updated_at=view.updated_at,
    )


def _invalid(exc: CommandValidationError) -> HTTPException:
    return HTTPException(status_code=422, detail={"field": exc.field, "message": str(exc)})


@router.get("/workflows", response_model=list[WorkflowOut])
async def list_workflows(request: Request) -> list[WorkflowOut]:
    """Every saved workflow, disabled ones included — this is the editor, not the menu."""
    views = await deps.command_store(request).list_commands(OPERATOR_ID)
    return [_workflow_out(view) for view in views]


@router.post("/workflows", status_code=201, response_model=WorkflowOut)
async def create_workflow(request: Request, body: WorkflowCreate) -> WorkflowOut:
    try:
        view = await deps.command_store(request).create(
            OPERATOR_ID,
            name=body.name,
            body=body.body,
            title=body.title,
            description=body.description,
            argument_hint=body.argument_hint,
            enabled=body.enabled,
        )
    except CommandValidationError as exc:
        raise _invalid(exc) from None
    return _workflow_out(view)


@router.get("/workflows/{command_id}", response_model=WorkflowOut)
async def get_workflow(request: Request, command_id: str) -> WorkflowOut:
    try:
        return _workflow_out(await deps.command_store(request).get(OPERATOR_ID, command_id))
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None


@router.patch("/workflows/{command_id}", response_model=WorkflowOut)
async def update_workflow(
    request: Request, command_id: str, body: WorkflowUpdate
) -> WorkflowOut:
    try:
        view = await deps.command_store(request).update(
            OPERATOR_ID,
            command_id,
            name=body.name,
            body=body.body,
            title=body.title,
            description=body.description,
            argument_hint=body.argument_hint,
            enabled=body.enabled,
        )
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except CommandValidationError as exc:
        raise _invalid(exc) from None
    return _workflow_out(view)


@router.delete("/workflows/{command_id}", status_code=204)
async def delete_workflow(request: Request, command_id: str) -> None:
    try:
        await deps.command_store(request).delete(OPERATOR_ID, command_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
