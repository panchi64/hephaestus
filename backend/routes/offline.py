"""Offline mode — the operator's read + control of connectivity-aware web suspension.

A thin surface over :mod:`services.offline`: report the live state and flip the two
switches (the manual force-offline toggle and the auto-detect master switch). All the
policy — the connectivity verdict, the web-service shutdown, the web-tool gate — lives in
the service; this just parses and relays.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel

from routes import deps
from services.offline import OfflineState

router = APIRouter(prefix="/offline", tags=["offline"])


class OfflineStatus(BaseModel):
    """The full read of offline mode the UI renders (`effective_offline` is what the
    web rows reflect; `online` is the raw connectivity verdict)."""

    manual_offline: bool
    auto_detect: bool
    online: bool
    effective_offline: bool


class OfflineUpdate(BaseModel):
    """Either switch, omitted ⇒ unchanged. Both may be set in one call."""

    manual_offline: bool | None = None
    auto_detect: bool | None = None


def _to_status(state: OfflineState) -> OfflineStatus:
    return OfflineStatus(
        manual_offline=state.manual,
        auto_detect=state.auto,
        online=state.online,
        effective_offline=state.effective_offline,
    )


@router.get("", response_model=OfflineStatus)
async def get_offline(request: Request) -> OfflineStatus:
    return _to_status(deps.offline(request).state())


@router.put("", response_model=OfflineStatus)
async def update_offline(body: OfflineUpdate, request: Request) -> OfflineStatus:
    svc = deps.offline(request)
    state = svc.state()
    if body.manual_offline is not None:
        state = await svc.set_manual(body.manual_offline)
    if body.auto_detect is not None:
        state = await svc.set_auto(body.auto_detect)
    return _to_status(state)
