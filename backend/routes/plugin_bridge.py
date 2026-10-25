"""The plugin window bridge, as the host app reaches it on a window's behalf.

The host sends the window's bridge token in ``X-Plugin-Window``; the plugin is read from
that token, never from anything the window sends. A call that needs the operator comes
back as ``outcome: confirm`` with an id, and runs only when the operator's answer names it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Literal

from fastapi import APIRouter, Body, Header, HTTPException, Request
from pydantic import BaseModel, JsonValue

from core.exceptions import NotFoundError, PermissionDeniedError
from routes import deps
from routes.chat.operator_send import send_operator_message
from routes.chat.schemas import ChatCreate
from services.plugins.bridge.base import BridgeCall, BridgeCapability
from services.plugins.bridge.gate import BridgeGate
from services.plugins.bridge.registry import load_capabilities
from services.plugins.runtime import PluginError
from services.plugins.windows import WindowSession, WindowSessions

router = APIRouter(prefix="/plugins/bridge", tags=["plugins"])

type WindowToken = Annotated[str, Header(alias="X-Plugin-Window")]


class BridgeOutcome(BaseModel):
    outcome: Literal["done", "confirm"]
    result: JsonValue = None
    confirmation_id: str | None = None
    #: Completes "<window title> wants to …".
    summary: str | None = None


class ConfirmBody(BaseModel):
    #: Allow this kind of call for the plugin's current version without asking again.
    always: bool = False


def _window(request: Request, token: str) -> WindowSession:
    window = deps.service(request, WindowSessions).bridge(token)
    if window is None:
        # 410, not 404: the host reopens the window on it, and an unknown method is a 404.
        raise HTTPException(status_code=410, detail="this window is closed; open it again")
    return window


def _capability(window: WindowSession, method: str) -> BridgeCapability:
    capability = load_capabilities().get(method)
    if capability is None:
        raise NotFoundError(f"there is no bridge method {method!r}")
    if capability.permission not in window.permissions:
        raise PermissionDeniedError(
            f"{window.plugin} does not declare the {capability.permission!r} permission"
        )
    return capability


def _call(request: Request, window: WindowSession, params: Mapping[str, JsonValue]) -> BridgeCall:
    async def send_chat(prompt: str, conversation_id: str | None) -> str:
        sent = await send_operator_message(
            deps.app_of(request), ChatCreate(prompt=prompt, conversation_id=conversation_id)
        )
        return sent.conversation_id

    return BridgeCall(
        window=window,
        params=params,
        services=deps.app_state(request).services,
        send_chat=send_chat,
    )


async def _run(capability: BridgeCapability, call: BridgeCall) -> BridgeOutcome:
    try:
        result = await capability.handler(call)
    except PluginError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from None
    return BridgeOutcome(outcome="done", result=result)


@router.post("/confirmations/{confirmation_id}", response_model=BridgeOutcome)
async def confirm_call(
    request: Request, confirmation_id: str, body: ConfirmBody, token: WindowToken
) -> BridgeOutcome:
    """Run a call the operator allowed."""
    # Whoever can open a window holds its bridge token, an API token included; only the
    # operator's own session may answer for them.
    deps.require_operator(request)
    window = _window(request, token)
    gate = deps.service(request, BridgeGate)
    pending = gate.take(window, confirmation_id)
    if pending is None:
        raise NotFoundError("that request has expired; try again from the window")
    capability = _capability(window, pending.method)
    if body.always:
        await gate.remember(window, pending.confirmation.grant)
    return await _run(capability, _call(request, window, pending.params))


@router.post("/{method}", response_model=BridgeOutcome)
async def call_method(
    request: Request,
    method: str,
    token: WindowToken,
    params: Annotated[dict[str, JsonValue] | None, Body()] = None,
) -> BridgeOutcome:
    window = _window(request, token)
    capability = _capability(window, method)
    gate = deps.service(request, BridgeGate)
    gate.spend(window, method)
    call = _call(request, window, params or {})
    confirmation = await capability.asks(call)
    if confirmation is not None and not await gate.granted(window, confirmation.grant):
        return BridgeOutcome(
            outcome="confirm",
            confirmation_id=gate.ask(window, method, call.params, confirmation),
            summary=confirmation.summary,
        )
    return await _run(capability, call)
