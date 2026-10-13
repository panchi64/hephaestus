"""Projects — the operator's working directories, and the scope the app filters by.

The store is exported as both a service (the worktree layer and the scoped list queries
resolve it) and a capability (the `project` tools read it). The `WorktreeManager` and the
checkout holders go out the same way, because the workspace resolver runs *inside* a run
and reaches them through the bag like any other capability.

The host surface (`/host`) rides along here: it is stateless and owns no service, and
Projects is what both halves of it are about — naming an absolute directory on the
operator's own machine, and opening a file back out of one of them.
"""

from __future__ import annotations

from pathlib import Path

from core.api_scopes import ScopeClaim
from core.vault import Vault
from harness.manifest import FeatureManifest, FeatureRuntime, HarnessContext
from routes import host as host_routes
from routes import projects as project_routes
from routes import worktrees as worktree_routes
from routes.deps import OPERATOR_ID
from services.projects import ProjectStore, WorktreeManager
from services.projects.checkout import CheckoutHolders
from services.projects.legacy_branches import rename_legacy_branches
from services.settings_store import SettingsStore
from tools.projects import project_toolset


async def _build(ctx: HarnessContext) -> FeatureRuntime:
    store = ProjectStore(ctx.engine, ctx.vault, ctx.services.get(SettingsStore))
    # Outside `data_dir` on purpose — an approved host command is fenced against reads of
    # the whole data directory, so a worktree beneath it would be unreadable by the very
    # shell that has to build and test in it (`services/projects/worktree.py`).
    #
    # From `ctx.settings`, never `get_settings()`: this app has its own Settings (a test
    # app's are a throwaway temp dir), and reading the process-global here had every test
    # cutting real worktrees into the operator's own home directory.
    worktrees = WorktreeManager(ctx.settings.worktrees_dir)
    checkouts = CheckoutHolders()
    ctx.lifecycle.track("projects.legacy_branches", _rename_branches(store, ctx.vault))
    return FeatureRuntime(
        services=(store, worktrees, checkouts),
        capabilities=(store, worktrees, checkouts),
        state={"projects": store, "worktrees": worktrees},
    )


async def _rename_branches(store: ProjectStore, vault: Vault) -> None:
    """Project paths are sealed, so this waits for the vault."""
    await vault.unlocked_event.wait()
    for project in await store.list(OPERATOR_ID, include_archived=True):
        await rename_legacy_branches(Path(project.root_path))


MANIFEST = FeatureManifest(
    name="projects",
    routers=(project_routes.router, worktree_routes.router, host_routes.router),
    # `/host` is deliberately unclaimed: opening a native dialog on the operator's
    # machine — reading back an arbitrary absolute path, or launching an application on
    # one — is not something an inbound token should be able to do, so it stays denied
    # by default.
    api_scopes=(ScopeClaim("projects", ("/projects", "/worktrees")),),
    toolsets=(("project", project_toolset),),
    build=_build,
)
