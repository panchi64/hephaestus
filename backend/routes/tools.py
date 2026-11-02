"""The tool catalog surface — the operator's read + control of which tools the agent gets.

`AE-3.3`: the operator must be able to disable individual tools, and a disabled tool must
not be offered to or invoked by the agent. The enforcement half already exists (the
enabled gate in ``tools/toolsets.py``); this is the half that lets the operator *use* it.

- ``GET /tools`` — every registered tool, namespaced as the agent sees it, with its
  category, its description, whether it is currently enabled, and whether its category
  starts dormant (registered and allowed, but loaded only once the agent asks for the
  group). The catalog comes from the live toolset registry (``tools/catalog.py``), so it
  can never drift from what the agent actually runs against.
- ``PUT /tools/{name}`` — flip one tool. An unknown name is a 404 rather than a stored
  setting that disables nothing.
- ``GET /tools/approval-scopes`` — the subset that can *pause* a run for approval, which
  is the vocabulary a conversation grant or a scheduled task's pre-authorization may
  name. Served rather than mirrored because external tools are named from the operator's
  own registered servers and connectors, so the set is only knowable at runtime.

Offline mode's automatic web suspension is deliberately **not** folded into the
``enabled`` flag reported here: this surface reports the operator's own choice, which is
what they can act on. The two sets union at run time (``services/tool_policy``), so a web
tool the operator left enabled is still withheld while offline.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from routes import deps
from services.tool_policy import get_disabled_tools, set_tool_enabled
from tools.catalog import ToolInfo, approval_scopes, tool_catalog

router = APIRouter(prefix="/tools", tags=["tools"])


def assembled_catalog(request: Request) -> list[ToolInfo]:
    """The assembled catalog, dormancy included — one derivation for the listing, the
    toggle and the runtime counts, so a row cannot describe a tool the others don't."""
    return tool_catalog(deps.tool_categories(request), deps.dormant_category_names(request))


class ToolOut(BaseModel):
    """One row of the catalog."""

    name: str
    category: str
    description: str
    enabled: bool
    # Its category starts each conversation dormant — the tool is registered and allowed,
    # but the agent has to load the group before it can call it. Reported beside
    # `enabled` rather than folded into it: this is not the operator's switch, and a tool
    # that is merely out of reach until asked for is not a tool that was turned off.
    dormant: bool = False


class ToolUpdate(BaseModel):
    enabled: bool


def _out(info: ToolInfo, disabled: frozenset[str]) -> ToolOut:
    return ToolOut(
        name=info.name,
        category=info.category,
        description=info.description,
        enabled=info.name not in disabled,
        dormant=info.dormant,
    )


@router.get("", response_model=list[ToolOut])
async def list_tools(request: Request) -> list[ToolOut]:
    disabled = await get_disabled_tools(deps.settings_store(request), deps.OPERATOR_ID)
    return [_out(info, disabled) for info in assembled_catalog(request)]


class ApprovalScopeOut(BaseModel):
    """One tool that can pause a run, as the pre-authorization/grant surfaces show it."""

    name: str
    category: str
    description: str


@router.get("/approval-scopes", response_model=list[ApprovalScopeOut])
async def list_approval_scopes(request: Request) -> list[ApprovalScopeOut]:
    """Every tool an operator may pre-authorize or grant. Declared before the ``{name}``
    route below only for readability — the two never collide, since that one is a PUT."""
    scopes = await approval_scopes(
        deps.external(request),
        deps.OPERATOR_ID,
        deps.tool_categories(request),
        deps.gated_tools(request),
    )
    return [
        ApprovalScopeOut(name=s.name, category=s.category, description=s.description)
        for s in scopes
    ]


@router.put("/{name}", response_model=ToolOut)
async def update_tool(name: str, body: ToolUpdate, request: Request) -> ToolOut:
    info = next((t for t in assembled_catalog(request) if t.name == name), None)
    if info is None:
        raise HTTPException(status_code=404, detail=f"tool {name!r} not found")
    disabled = await set_tool_enabled(
        deps.settings_store(request), deps.OPERATOR_ID, name, body.enabled
    )
    return _out(info, disabled)
