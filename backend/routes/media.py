"""Remote-image proxy — the one way a picture from the open web reaches an answer.

An answer's Markdown can carry an image (see ``prompts.agent``), but the frontend
never points an ``<img>`` at the remote URL: it points here, same-origin, and this
route fetches the bytes through :mod:`services.webimage`. That is the whole purpose —
an ``<img>`` fetches on render with no click, so pointing one at a host named by
relayed page content would leak the operator's address, user-agent, and cookies to
that host silently. One hop through the backend removes all three.

The bytes are served **inert**, on the same principle as ``/views``: a sniffed content
type (never the remote's claim), ``nosniff``, a no-privileges CSP, and an attachment
disposition, so a response that turns out to be something other than the picture it
claimed cannot become a document with a foothold on the API origin.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request, Response

from core.exceptions import SSRFError
from routes import deps
from services.webimage import ImageFetchError, fetch_image

router = APIRouter(prefix="/media", tags=["media"])

# Untrusted third-party bytes. `nosniff` pins the browser to the type we sniffed;
# the CSP grants the response nothing at all, so even a crafted payload that slipped
# past the format check has no script, no fetch, and no framing available to it.
_IMAGE_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; sandbox",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    # Same URL, same bytes: the remote image is immutable for our purposes, and a
    # cache spares a re-fetch every time the transcript re-renders. Private — this is
    # the operator's browser, not a shared cache.
    "Cache-Control": "private, max-age=86400",
}


@router.get("/remote-image")
async def remote_image(
    request: Request,
    url: Annotated[str, Query(description="Absolute http(s) URL of the image to fetch.")],
) -> Response:
    """Fetch ``url`` server-side and return it as an image, or fail plainly.

    Every failure is a 4xx with a short reason rather than a placeholder image: the
    frontend renders a caption in the answer's own voice for a picture that didn't
    load, and a fake image would be a worse lie than an honest gap.
    """
    settings = deps.settings(request)
    try:
        image = await fetch_image(
            url,
            client=deps.web_client(request),
            timeout_s=settings.web_image_timeout_s,
            max_bytes=settings.web_image_max_bytes,
        )
    except SSRFError as exc:
        # A refused target is a boundary, not a transient failure — say so distinctly
        # so the frontend never retries it.
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ImageFetchError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return Response(
        content=image.data,
        media_type=image.content_type,
        headers={**_IMAGE_HEADERS, "Content-Disposition": "attachment"},
    )
