"""View tools — the agent shows the operator what it built, in one versioned View.

A conversation has a single **View**: a canvas beside the chat that holds a history
of **versions** to compare against, optionally fronted by a live, interactive
**head**. A ``show`` is the only thing that mints a version: it captures the sandbox
workspace as a new version (the version's *code*, via ``services/workspace_history``)
and stamps **how it previews**:

- ``show(file=…)`` captures the file's bytes as the version's preview (via
  ``services/artifacts``) — an image, an HTML page, a snippet — rendered by kind.
- ``show(serve=…)`` runs a server in the sandbox and emits ``view.live`` — the
  backend reverse-proxies it into a sandboxed iframe — overlaid on the version.

Every version is one ``view.snapshot`` event carrying its preview descriptor; the
workspace tree behind it is the comparable, diffable code. If the sandbox or the
version store isn't wired into the run, the tool says so rather than failing — the
model adapts (graceful degradation).
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from pydantic_ai import FunctionToolset, ModelRetry, RunContext

from runs import ViewLive, ViewLiveStopped, ViewSnapshot
from services.artifacts import ArtifactStore
from services.sandbox import SandboxError, SandboxSessionManager
from services.workspace_history import SnapshotView, WorkspaceHistoryStore, format_show_result

from .deps import RunDeps
from .emit import RunEventEmitted

if TYPE_CHECKING:
    from services.sandbox.session import SandboxSession

logger = logging.getLogger(__name__)


async def _capture_version(
    ctx: RunContext[RunDeps],
    session: SandboxSession,
    *,
    title: str | None,
    preview_artifact_id: str | None,
    preview_kind: str | None,
) -> SnapshotView:
    """Capture the sandbox workspace as a new View version stamped with its preview,
    and emit the ``view.snapshot`` event. The caller has already guarded that the
    version store is wired."""
    history = ctx.deps.caps.get_optional(WorkspaceHistoryStore)
    assert history is not None  # guarded by the caller
    files = await asyncio.to_thread(session.collect_text_files)
    snapshot = await history.capture(
        ctx.deps.owner_id,
        ctx.deps.workspace_key,
        run_id=ctx.deps.run.id,
        files=files,
        title=title,
        preview_artifact_id=preview_artifact_id,
        preview_kind=preview_kind,
    )
    _ = await ctx.emit(
        RunEventEmitted(
            body=ViewSnapshot(
                conversation_id=ctx.deps.workspace_key,
                snapshot_id=snapshot.id,
                title=snapshot.title,
                created_at=snapshot.created_at,
                files_changed=snapshot.files_changed,
                summary=snapshot.summary,
                preview_kind=snapshot.preview_kind,
                preview_artifact_id=snapshot.preview_artifact_id,
            )
        )
    )
    return snapshot


async def _show_file(ctx: RunContext[RunDeps], file: str, title: str | None) -> str:
    """Mint a new View version whose preview is a file the agent produced: capture the
    file's bytes (the rendered preview) and the workspace tree (the version's code)."""
    sessions = ctx.deps.caps.get_optional(SandboxSessionManager)
    store = ctx.deps.caps.get_optional(ArtifactStore)
    history = ctx.deps.caps.get_optional(WorkspaceHistoryStore)
    if sessions is None or store is None or history is None:
        return "The view is unavailable."
    try:
        session = await sessions.acquire(ctx.deps.workspace_key, holder=ctx.deps.run)
        content = session.read_file(file)
    except SandboxError as exc:
        return f"Could not read {file!r}: {exc}"
    artifact = await store.publish(
        ctx.deps.owner_id,
        ctx.deps.workspace_key,
        filename=file.rsplit("/", 1)[-1],
        content=content,
        title=title,
        run_id=ctx.deps.run.id,
    )
    try:
        snapshot = await _capture_version(
            ctx,
            session,
            title=title or artifact.filename,
            preview_artifact_id=artifact.id,
            preview_kind=artifact.kind,
        )
    except Exception:  # noqa: BLE001 — a history failure must never break the turn
        logger.warning("view version capture failed", exc_info=True)
        return f"Showed '{title or artifact.filename}', but couldn't record a comparable version."
    return format_show_result(snapshot, artifact.kind)


async def _show_live(
    ctx: RunContext[RunDeps],
    serve: list[str],
    port: int | None,
    path: str | None,
    title: str | None,
) -> str:
    """Run a server as the View's live, interactive head — overlaying a fresh version
    of the workspace, so the head's code is recorded and comparable like any other."""
    sessions = ctx.deps.caps.get_optional(SandboxSessionManager)
    if sessions is None:
        return "The live view is unavailable — your computer isn't available right now."
    if port is None:
        raise ModelRetry("`serve` needs the `port` the server listens on.")
    try:
        handle = await sessions.start_preview(ctx.deps.workspace_key, serve, port)
    except SandboxError as exc:
        return f"The live server did not start: {exc}"
    # Announce the running head first — point the iframe at the entry path so a static
    # server whose root would list the directory (`python -m http.server` with no
    # `index.html`) renders the page instead.
    url = handle.url_for(path)
    _ = await ctx.emit(
        RunEventEmitted(
            body=ViewLive(
                conversation_id=ctx.deps.workspace_key,
                url=url,
                title=title,
                command=" ".join(handle.command),
                port=port,
            )
        )
    )
    # A live head overlays the latest version; capture one now (auto preview — the
    # frontend renders the running server) so the head has real, comparable code behind
    # it. Best-effort and *after* the live event: a capture failure must never orphan
    # the already-running server or hide it from the operator. The live head carries no
    # static preview, so it folds no separate version chip — the LIVE chip represents it.
    if ctx.deps.caps.get_optional(WorkspaceHistoryStore) is not None:
        try:
            session = await sessions.acquire(ctx.deps.workspace_key, holder=ctx.deps.run)
            _ = await _capture_version(
                ctx, session, title=title, preview_artifact_id=None, preview_kind=None
            )
        except Exception:  # noqa: BLE001 — history is best-effort, never break the turn
            logger.warning("live-view version capture failed", exc_info=True)
    return f"Live view running at {url} (serving '{' '.join(handle.command)}')."


def view_toolset() -> FunctionToolset[RunDeps]:
    toolset: FunctionToolset[RunDeps] = FunctionToolset()

    @toolset.tool
    async def show(
        ctx: RunContext[RunDeps],
        title: str | None = None,
        file: str | None = None,
        serve: list[str] | None = None,
        port: int | None = None,
        path: str | None = None,
    ) -> str:
        """Show the operator what you built, in this conversation's View — one canvas
        beside the chat with a version history to compare against.

        Pass exactly one of:
        - ``file``: a file you made — an HTML page, an image or chart, a code snippet.
          Becomes a new comparable version.
        - ``serve`` + ``port``: the argv of a live server (``["npm", "run", "dev"]``)
          and the port it listens on. Becomes the interactive head, replacing any
          already running here, and returns once it is up. The server must bind
          ``0.0.0.0``, not ``127.0.0.1``, and serve assets with relative URLs; if its
          root would show a directory listing, pass ``path`` for the entry file
          (``"index.html"``).

        ``title`` labels the View. Use this to *show* a result, not to store data."""
        if (file is None) == (serve is None):
            raise ModelRetry(
                "Pass exactly one of `file` (show a file you made) or `serve` (run a "
                + "live server) — not both, not neither."
            )
        if serve is not None:
            return await _show_live(ctx, serve, port, path, title)
        assert file is not None  # narrowed by the exactly-one check above
        return await _show_file(ctx, file, title)

    @toolset.tool
    async def close(ctx: RunContext[RunDeps]) -> str:
        """Stop the live head of this conversation's View, if one is running. The
        saved versions stay; this only tears down the running server."""
        sessions = ctx.deps.caps.get_optional(SandboxSessionManager)
        if sessions is None:
            return "The live view is unavailable — your computer isn't available right now."
        await sessions.stop_preview(ctx.deps.workspace_key)
        _ = await ctx.emit(
            RunEventEmitted(body=ViewLiveStopped(conversation_id=ctx.deps.workspace_key))
        )
        return "Stopped the live view."

    return toolset
