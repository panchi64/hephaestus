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

from core.diagnostics import render_tool_error
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
        ctx.deps.scratch_key,
        run_id=ctx.deps.run.id,
        files=files,
        title=title,
        preview_artifact_id=preview_artifact_id,
        preview_kind=preview_kind,
    )
    _ = await ctx.emit(
        RunEventEmitted(
            body=ViewSnapshot(
                conversation_id=ctx.deps.scratch_key,
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
        session = await sessions.acquire(ctx.deps.scratch_key, holder=ctx.deps.run)
        content = session.read_file(file)
    except SandboxError as exc:
        return f"Could not read {file!r}: {exc}"
    artifact = await store.publish(
        ctx.deps.owner_id,
        ctx.deps.scratch_key,
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
    except Exception:  # a history failure must never break the turn
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
        raise ModelRetry(
            render_tool_error(
                "`serve` was passed without a `port`",
                hints=["pass the `port` the server listens on"],
            )
        )
    try:
        handle = await sessions.start_preview(ctx.deps.scratch_key, serve, port)
    except SandboxError as exc:
        return f"The live server did not start: {exc}"
    # Announce the running head first — point the iframe at the entry path so a static
    # server whose root would list the directory (`python -m http.server` with no
    # `index.html`) renders the page instead.
    url = handle.url_for(path)
    _ = await ctx.emit(
        RunEventEmitted(
            body=ViewLive(
                conversation_id=ctx.deps.scratch_key,
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
            session = await sessions.acquire(ctx.deps.scratch_key, holder=ctx.deps.run)
            _ = await _capture_version(
                ctx, session, title=title, preview_artifact_id=None, preview_kind=None
            )
        except Exception:  # history is best-effort, never break the turn
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
        """Shows the operator something you built in this conversation's View: a panel
        beside the chat that keeps every version you show, so they can step back and
        compare.

        Use it when seeing the result beats reading about it: a page, a chart, an image,
        a running app. Show it once it works, then tell the operator in a line what they
        are looking at rather than describing it at length. Each call records a new
        version, so show meaningful milestones rather than every small edit. It is for
        showing a result, not for storing data or handing over a file.

        Pass exactly one of:
        - `file`: a file you made, as a path relative to `~/scratch` (`chart.png`,
          `site/index.html`). It renders by kind: an HTML page, an image, a code
          snippet. Only that one file is captured, so a page that loads scripts or
          styles from neighboring files needs them inlined, or `serve` instead. A
          project file has to be copied into `~/scratch` first.
        - `serve` with `port`: the argv of a server to run from `~/scratch`
          (`["python", "-m", "http.server", "8000"]`) and the port it listens on. It
          becomes the View's live, interactive head, replacing any server already shown
          here, and the call returns once the server answers. The server must bind
          `0.0.0.0`, not `127.0.0.1`, because the View reaches it from outside your
          machine. It must load its assets by relative URL, because the View serves it
          under a path of its own and a root-relative `/app.js` misses. If its root
          would show a directory listing, pass `path` for the entry page (`index.html`).

        Returns a short confirmation, or the reason it could not show the result: a
        missing file, or the server's own log when it exited without serving.

        Args:
            title: A short label for this version, shown in the View.
            file: The file to show, relative to `~/scratch`.
            serve: The server's argv, one list item per argument.
            port: The port the server listens on. Required with `serve`.
            path: The page to open on the live server, relative to its root.
        """
        if (file is None) == (serve is None):
            raise ModelRetry(
                render_tool_error(
                    "pass exactly one of `file` or `serve`",
                    hints=["`file` shows a file you made; `serve` runs a live server"],
                )
            )
        if serve is not None:
            return await _show_live(ctx, serve, port, path, title)
        assert file is not None  # narrowed by the exactly-one check above
        return await _show_file(ctx, file, title)

    @toolset.tool
    async def close(ctx: RunContext[RunDeps]) -> str:
        """Stops the live server shown in this conversation's View, if one is running.

        Use it when the operator is done with the running app or asks you to stop it.
        The saved versions stay in the View; only the server goes. A new `view_show`
        with `serve` replaces the running server on its own, so you do not need this
        before showing a new one.
        """
        sessions = ctx.deps.caps.get_optional(SandboxSessionManager)
        if sessions is None:
            return "The live view is unavailable — your computer isn't available right now."
        await sessions.stop_preview(ctx.deps.scratch_key)
        _ = await ctx.emit(
            RunEventEmitted(body=ViewLiveStopped(conversation_id=ctx.deps.scratch_key))
        )
        return "Stopped the live view."

    return toolset
