"""The agent's browser, from the app's side: its session, the live view, and the profile.

The browser runs headless in the platform VM. A conversation's session is opened and
ended here, watched as a stream of screencast frames, and driven by posting input events;
all of it rides the app's authenticated HTTP surface under the chat scope.

``GET /browser/session/{conversation_id}`` is read-only on purpose: it never touches the
session, so a UI that checks it cannot keep an otherwise-idle browser alive.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from core.sse import sse_stream
from routes import deps
from services.browser.view import ViewInput, dispatch, frames

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/browser", tags=["browser"])

#: Shown verbatim in a toast.
_UNAVAILABLE = "The browser is not available: the platform VM or web fetch is not running."
_NO_SESSION = "This conversation has no open browser."


class BrowserSessionInfo(BaseModel):
    """The live browser for a conversation, or ``active=False`` when there is none."""

    active: bool
    page_url: str | None = None


@router.get("/session/{conversation_id}", response_model=BrowserSessionInfo)
async def session_info(request: Request, conversation_id: str) -> BrowserSessionInfo:
    sessions = deps.browser_sessions(request)
    live = sessions.existing(conversation_id) if sessions is not None else None
    if live is None:
        return BrowserSessionInfo(active=False)
    return BrowserSessionInfo(active=True, page_url=live.page_url or None)


@router.post("/session/{conversation_id}", response_model=BrowserSessionInfo)
async def open_session(request: Request, conversation_id: str) -> BrowserSessionInfo:
    """Open this conversation's browser. Idempotent; a 503 says why none could open."""
    sessions = deps.browser_sessions(request)
    live = await sessions.acquire(conversation_id) if sessions is not None else None
    if live is None:
        raise HTTPException(status_code=503, detail=_UNAVAILABLE)
    return BrowserSessionInfo(active=True, page_url=live.page_url or None)


@router.delete("/session/{conversation_id}", status_code=204)
async def close_session(request: Request, conversation_id: str) -> Response:
    """The operator ends this conversation's browser; the agent is told they did."""
    sessions = deps.browser_sessions(request)
    if sessions is not None:
        await sessions.close(conversation_id)
    return Response(status_code=204)


@router.get("/session/{conversation_id}/view")
async def view(request: Request, conversation_id: str):
    """Screencast frames of the session's current page, as server-sent events."""
    sessions = deps.browser_sessions(request)
    live = sessions.existing(conversation_id) if sessions is not None else None
    if sessions is None or live is None:
        raise HTTPException(status_code=404, detail=_NO_SESSION)

    def alive() -> bool:
        return sessions.existing(conversation_id) is live

    return sse_stream(lambda: frames(live, alive), _frame)


@router.post("/session/{conversation_id}/input", status_code=204)
async def send_input(request: Request, conversation_id: str, event: ViewInput) -> Response:
    """Replay one mouse, wheel, keyboard or navigation input on the current page."""
    sessions = deps.browser_sessions(request)
    live = sessions.existing(conversation_id) if sessions is not None else None
    if live is None:
        raise HTTPException(status_code=404, detail=_NO_SESSION)
    try:
        await dispatch(live, event)
    except Exception as exc:
        logger.debug("browser view: input %s failed", event.type, exc_info=True)
        reason = (str(exc).splitlines() or [""])[0][:300]
        raise HTTPException(
            status_code=502, detail=f"The browser did not take that input: {reason}"
        ) from exc
    return Response(status_code=204)


@router.delete("/profile", status_code=204)
async def forget_profile(request: Request) -> Response:
    """Log the shared browser profile out of everything: every session ends unsaved."""
    sessions = deps.browser_sessions(request)
    if sessions is not None:
        await sessions.forget_profile()
    return Response(status_code=204)


def _frame(item: dict[str, Any]) -> str:
    return f"data: {json.dumps(item, separators=(',', ':'))}\n\n"
