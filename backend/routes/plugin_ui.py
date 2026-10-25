"""The pages of open plugin windows, served from ``/plugin-ui/{page token}/…``.

The token is the credential, so this prefix skips the cookie gate the way ``/previews``
does; it stays behind the vault gate. Every response carries a sandboxing CSP without
``allow-same-origin``: the page runs in an opaque origin however it is opened, can load
only the window's own files, and can reach no network. It talks to the app only through
the message port the host hands its frame.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path

from fastapi import APIRouter, Request, Response

from core.exceptions import NotFoundError
from routes import deps
from services.plugins.contributions.windows import content_type
from services.plugins.windows import WindowSessions

router = APIRouter(prefix="/plugin-ui", tags=["plugins"])

#: The reserved path every window page can load the bridge SDK from.
BRIDGE_SCRIPT = "__bridge.js"
_SDK = Path(__file__).resolve().parent.parent / "services" / "plugins" / "ui_bridge.js"


@cache
def _sdk() -> bytes:
    return _SDK.read_bytes()


def _headers(request: Request, media_type: str) -> dict[str, str]:
    ancestors = " ".join(["'self'", *deps.settings(request).cors_origins])
    return {
        "Content-Type": media_type,
        "Content-Security-Policy": (
            "sandbox allow-scripts; default-src 'none'; "
            + "script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
            + "img-src 'self' data: blob:; font-src 'self' data:; media-src 'self' data: blob:; "
            + "connect-src 'none'; form-action 'none'; base-uri 'none'; "
            + f"frame-ancestors {ancestors}"
        ),
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "Cache-Control": "no-store",
    }


@router.get("/{token}/{path:path}")
async def window_file(request: Request, token: str, path: str) -> Response:
    session = deps.service(request, WindowSessions).page(token)
    if session is None:
        raise NotFoundError("this window is closed")
    if path == BRIDGE_SCRIPT:
        return Response(_sdk(), headers=_headers(request, "text/javascript; charset=utf-8"))
    blob = session.files.get(path)
    media_type = content_type(path)
    if blob is None or media_type is None:
        raise NotFoundError("no such file in this window")
    return Response(blob, headers=_headers(request, media_type))
