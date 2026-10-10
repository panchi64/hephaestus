"""The conversation's one versioned output surface: live head and snapshots."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from .base import _Body


# The View is one canvas with a history of **versions** to compare plus an optional
# live **head**. A version is one ``view.snapshot`` (the captured workspace tree +
# how it previews); ``view.live`` / ``view.live.stopped`` are the interactive head
# overlaid on the latest version.
class ViewLive(_Body):
    """The agent started (or replaced) the View's live head — a running server.
    ``url`` is a token-gated proxy path on this same API origin
    (``/previews/{token}/…``) that streams the server's HTTP and WebSocket traffic
    out of the sandbox.

    Frontend contract: mount it as ``<iframe src={url}>`` with
    ``sandbox="allow-scripts allow-forms allow-popups"`` — deliberately **without**
    ``allow-same-origin``, so the framed (model-generated) app runs in an opaque
    origin and cannot act as the operator against the API. The token in the path is
    the credential, so no auth header is needed and relative subresources/WebSockets
    resolve automatically. ``url`` already carries the entry path when one was given,
    so it renders the page rather than a directory listing. Additive to v1; no bump."""

    type: Literal["view.live"] = "view.live"
    conversation_id: str
    url: str  # "/previews/{token}/<entry>"
    title: str | None = None
    command: str  # the server command, for display
    port: int  # the in-container port it listens on


class ViewLiveStopped(_Body):
    """The View's live head was torn down (explicitly via close_view, or reaped with
    its idle session); the frontend drops the live iframe for this conversation."""

    type: Literal["view.live.stopped"] = "view.live.stopped"
    conversation_id: str


class ViewSnapshot(_Body):
    """A new **version** of the conversation's View — minted by a ``show``. It captures
    the agent's sandbox tree (the version's code, browsable + diffable via
    ``/views/snapshots/{snapshot_id}/…``) and how it previews: ``preview_artifact_id``
    + ``preview_kind`` point at the captured-bytes preview of a ``show(file=…)`` (fetch
    them from ``/views/{preview_artifact_id}/content``), or both are null for a
    live/auto preview (a running head, or the frontend auto-picks an entry HTML page).
    Additive to v1; no bump."""

    type: Literal["view.snapshot"] = "view.snapshot"
    conversation_id: str
    snapshot_id: str
    title: str | None = None
    created_at: datetime
    files_changed: int
    summary: str  # compact change tally, e.g. "+2 ~1 −0"
    preview_kind: str | None = None  # "html" | "image" | "text" | "other" | None
    preview_artifact_id: str | None = None
