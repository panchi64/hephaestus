"""The built-in plugins, and the operator's switch for each."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from harness.builtin_plugins import BuiltinPlugins, BuiltinPluginView
from routes import deps
from services.plugins.contributions.base import Described

router = APIRouter(prefix="/plugins/builtin", tags=["plugins"])


class BuiltinPluginOut(BaseModel):
    name: str
    title: str
    description: str
    enabled: bool
    #: Every contribution by type key, as each type describes its items.
    contributions: dict[str, list[Described]]


class BuiltinPluginSwitch(BaseModel):
    enabled: bool


def _out(view: BuiltinPluginView) -> BuiltinPluginOut:
    return BuiltinPluginOut(
        name=view.name,
        title=view.title,
        description=view.description,
        enabled=view.enabled,
        contributions=view.contributions,
    )


@router.get("", response_model=list[BuiltinPluginOut])
async def list_builtin(request: Request) -> list[BuiltinPluginOut]:
    return [_out(view) for view in deps.service(request, BuiltinPlugins).views()]


@router.put("/{name}", response_model=BuiltinPluginOut)
async def switch_builtin(
    name: str, body: BuiltinPluginSwitch, request: Request
) -> BuiltinPluginOut:
    plugins = deps.service(request, BuiltinPlugins)
    if not plugins.is_plugin(name):
        raise HTTPException(status_code=404, detail="no built-in plugin by that name")
    await plugins.set_enabled(name, body.enabled)
    return next(_out(view) for view in plugins.views() if view.name == name)
