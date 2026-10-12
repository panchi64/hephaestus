"""The View feature — one versioned output surface (head = live preview, history =
workspace snapshots).

`ArtifactStore` holds a version's preview bytes (capture-encrypted, served inert);
`WorkspaceHistoryStore` is the git-style version history a `view_show` snapshots
into; the previews router reverse-proxies the live head out of the sandbox.
"""

from __future__ import annotations

import httpx

from core.api_scopes import ScopeClaim
from harness.manifest import FeatureManifest, FeatureRuntime, HarnessContext
from routes import previews as previews_routes
from routes import views as views_routes
from services.artifacts import ArtifactStore
from services.workspace_history import WorkspaceHistoryStore
from tools.view import view_toolset


async def _build(ctx: HarnessContext) -> FeatureRuntime:
    # The View's static versions — the agent captures a sandbox file here, the
    # frontend fetches and renders it on the View canvas. Encrypted at rest like the
    # rest of the operator's data. (The live head rides the sandbox + the /previews
    # proxy; this store is the snapshot/version history.)
    artifacts = ArtifactStore(ctx.engine, ctx.vault)
    # The git-style history — after a file-changing turn the sandbox workspace is
    # captured as a content-addressed, encrypted snapshot; the frontend browses each
    # version's code and diffs it against the previous one.
    workspace_history = WorkspaceHistoryStore(ctx.engine, ctx.vault)
    # Reused by the preview reverse proxy to forward HTTP to a sandbox server. No
    # redirect following — the proxy rewrites Location and returns it to the browser.
    preview_client = httpx.AsyncClient(follow_redirects=False)
    ctx.lifecycle.on_stop("preview-client", preview_client.aclose)
    return FeatureRuntime(
        services=(artifacts, workspace_history),
        capabilities=(artifacts, workspace_history),
        state={
            "artifacts": artifacts,
            "workspace_history": workspace_history,
            "preview_client": preview_client,
        },
    )


MANIFEST = FeatureManifest(
    name="views",
    routers=(views_routes.router, previews_routes.router),
    # A View's versions ride the chat scope — they are part of reading a thread.
    api_scopes=(ScopeClaim("chat", ("/views",)),),
    toolsets=(("view", view_toolset),),
    # The live-head proxy is auth-exempt: the unguessable token in the path is the
    # credential (a sandboxed, opaque-origin iframe loads it without the operator
    # cookie), and it only ever proxies to a loopback preview server.
    public_prefixes=("/previews",),
    build=_build,
)
