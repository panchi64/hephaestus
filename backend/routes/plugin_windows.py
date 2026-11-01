"""The windows published plugins offer, and opening one."""

from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel

from core.exceptions import NotFoundError
from routes import deps
from routes.deps import OPERATOR_ID
from services.plugins.manifest import PluginWindow
from services.plugins.windows import WindowSessions

router = APIRouter(prefix="/plugins", tags=["plugins"])


class WindowOut(BaseModel):
    plugin_id: str
    plugin: str
    window: str
    title: str
    width: int
    height: int
    min_width: int
    min_height: int
    resizable: bool


class WindowSessionOut(BaseModel):
    #: The page's path on this API, token included.
    src: str
    #: What the host sends with each bridge call it makes for this window.
    bridge_token: str
    permissions: list[str]


def window_out(plugin_id: str, plugin: str, window: PluginWindow) -> WindowOut:
    return WindowOut(
        plugin_id=plugin_id,
        plugin=plugin,
        window=window.id,
        title=window.title,
        width=window.width,
        height=window.height,
        min_width=window.min_width,
        min_height=window.min_height,
        resizable=window.resizable,
    )


@router.get("/windows", response_model=list[WindowOut])
async def list_windows(request: Request) -> list[WindowOut]:
    return [
        window_out(view.id, view.name, window)
        for view in await deps.plugins(request).live(OPERATOR_ID)
        if view.active is not None
        for window in view.active.windows
    ]


@router.post(
    "/{plugin_id}/windows/{window_id}/sessions",
    response_model=WindowSessionOut,
    status_code=201,
)
async def open_window(request: Request, plugin_id: str, window_id: str) -> WindowSessionOut:
    store = deps.plugins(request)
    view = await store.get(OPERATOR_ID, plugin_id)
    window = None if view.active is None or not view.published else view.active.window(window_id)
    if view.active is None or window is None:
        raise NotFoundError(f"{view.name} has no published window {window_id!r}")
    session = deps.service(request, WindowSessions).open(
        owner_id=OPERATOR_ID,
        plugin_id=view.id,
        plugin=view.name,
        window=window,
        permissions=view.active.permissions,
        files=await store.files(OPERATOR_ID, plugin_id, "active"),
    )
    return WindowSessionOut(
        src=session.src,
        bridge_token=session.bridge_token,
        permissions=sorted(session.permissions),
    )
