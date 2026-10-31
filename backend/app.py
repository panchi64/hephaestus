"""FastAPI application assembly: middleware, auth, routers and the singletons on ``app.state``.

The process boots locked: only the vault, the auth gate and what holds no operator data
come up here. The database, the encrypted volume and every unit over them open at unlock
(`harness.workspace`). Business logic lives below this layer. See
``docs/architecture/README.md``.
"""

from __future__ import annotations

import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

import core.volume
from core.api_scopes import CORE_CLAIMS, ScopeTable
from core.auth import AuthManager, AuthMiddleware
from core.config import Settings, get_settings
from core.devserver import UNGUARDED_RELOAD_WARNING, reload_watches_runtime_state
from core.http_errors import install_error_handlers
from core.shutdown import begin_shutdown, reset_shutdown
from core.vault import Vault
from core.workspace import WorkspaceOpenError
from harness import LifecycleRegistry
from harness.builtin_plugins import (
    gate_availability,
    gate_instruction,
    gate_prompt_context,
    plugin_gate,
)
from harness.discovery import discover_manifests
from harness.manifest import DormantCategory
from harness.workspace import WorkspaceSession
from routes import (
    api_tokens,
    auth,
    chat,
    conversations,
    health,
    models,
    onboarding,
    overview,
    runs,
    tokens,
    tools,
    vm,
)
from routes.deps import OPERATOR_ID, AppState, app_state
from services.onboarding import mark_pending as mark_onboarding_pending
from services.sandbox import (
    resolve_confinement,
    shutdown_confinement,
    stop_all_groups,
)
from services.tool_policy import AvailabilityCheck, CategoryAvailability
from services.vm import GuestImage, Platform
from tools import (
    CORE_GATED_TOOLS,
    InstructionProvider,
    PromptContextProvider,
    core_categories,
)
from tools.describe import category_names
from tools.tasks import tasks_context
from tools.workspace_context import workspace_context

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Process-lifetime startup and shutdown."""
    settings = app_state(app).settings
    # A unit registers at its construction point and shutdown unwinds in reverse order;
    # something that must stop earlier than that implies simply registers later.
    lifecycle = LifecycleRegistry()
    # `_wire` starts long-lived units as it goes, so it sits inside the `try`: a build that
    # fails half-way must unwind the units already started, or the VM, drainers and proxy
    # listeners keep running with no reference.
    try:
        await _wire(app, settings, lifecycle)
        yield
    finally:
        # Raised before anything stops, so every long-lived response (notification feed,
        # parked run stream, proxied preview) ends. A signal already raised it from
        # `core.server`; this covers paths without one (test client, failed boot). Cleared
        # last so the next app in this process starts clear.
        begin_shutdown()
        try:
            await lifecycle.stop_all()
        finally:
            reset_shutdown()


async def _wire(app: FastAPI, settings: Settings, lifecycle: LifecycleRegistry) -> None:
    """Build the process-scoped units and the closed workspace, opening it when a boot
    passphrase is configured."""
    state = app_state(app)
    state.auth_manager = AuthManager()
    # Inbound scoped API tokens, read by the auth gate on every request; None until the
    # workspace opens, since the token table lives in the encrypted database.
    state.api_tokens = None

    settings.data_dir.mkdir(parents=True, exist_ok=True)
    # `uvicorn app:app --reload` watches the data directory and restarts the server when
    # runtime state changes; the reloader is the parent process, so only a warning is possible.
    if reload_watches_runtime_state(sys.argv, settings.data_dir):
        logger.warning(UNGUARDED_RELOAD_WARNING, settings.data_dir)
    # Read before anything creates the database. The keyfile sits beside it, so with a
    # cleared `app.db` the operator would still be asked to unlock a key protecting
    # nothing; `/auth/status` reports both as `db_missing`. An explicit `db_url` counts as
    # intact (we don't own its path).
    db_path = None if settings.db_url else settings.data_dir / "app.db"
    state.workspace_db_intact = db_path.exists() if db_path else True

    vault = Vault(settings.data_dir / "keyfile.json")
    state.vault = vault
    # First registered, so it runs last: the key is zeroed after everything has stopped.
    lifecycle.on_stop("vault", vault.lock)

    # Pooled client for provider model discovery; follows redirects (some providers 30x).
    discovery_client = httpx.AsyncClient(follow_redirects=True)
    state.discovery_client = discovery_client
    lifecycle.on_stop("discovery-client", discovery_client.aclose)
    # Confinement starts proxy listeners, which would accumulate per restart under the
    # reloading dev server; resolved now so `confinement_state` is settled before the first command.
    lifecycle.on_stop("host-confinement", shutdown_confinement)
    _ = await resolve_confinement(settings)
    # Catches stray process groups from backgrounded servers; stops after the runs, which
    # kill their own commands.
    lifecycle.on_stop("host-process-groups", stop_all_groups)
    # The guest image holds no operator data, so it builds while the vault is still locked;
    # until it is ready, what needs a machine reports unavailable rather than using the host.
    vm_dir = settings.data_dir / "vm"
    platform = Platform(
        GuestImage(
            vm_dir / "images",
            ca_bundle=Path(settings.vm_ca_bundle) if settings.vm_ca_bundle else None,
            override=Path(settings.vm_image) if settings.vm_image else None,
        ),
        enabled=settings.vm_enabled,
    )
    await lifecycle.start("vm-image", start=platform.start, stop=platform.stop)

    volume = core.volume.select_volume(settings)
    # A crashed process can leave the volume mounted, decrypted, while this one is locked.
    await core.volume.close_volume(volume)
    workspace = WorkspaceSession(
        app,
        settings,
        vault=vault,
        volume=volume,
        platform=platform,
        discovery_client=discovery_client,
    )
    state.workspace = workspace
    # Registered last so it stops first, while the VM platform and confinement still run.
    lifecycle.on_stop("workspace", workspace.close)
    if settings.unlock_passphrase:
        await _open_with_boot_passphrase(app, settings.unlock_passphrase)


async def _open_with_boot_passphrase(app: FastAPI, passphrase: str) -> None:
    """The auth-disabled path: set up or unlock the vault at boot, then open the workspace.

    A workspace that fails to open leaves the process up and locked, so the reason reaches
    `/auth/status` instead of a crash loop.
    """
    state = app_state(app)
    vault, workspace = state.vault, state.workspace
    created = not vault.is_initialized
    if created:
        await vault.setup(passphrase)
    elif not await vault.unlock(passphrase):
        logger.warning("vault: the boot passphrase does not unlock this workspace")
        return
    try:
        await workspace.open()
    except WorkspaceOpenError:
        vault.lock()
        return
    if created:
        await mark_onboarding_pending(state.settings_store, OPERATOR_ID)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(title="Hephaestus", version=settings.version, lifespan=lifespan)
    app.state = AppState()
    state = app_state(app)
    state.settings = settings  # the lifespan reads this (tests inject it)
    state.feature_manifests = discover_manifests()
    enabled_manifests = [
        m for m in state.feature_manifests if m.enabled is None or m.enabled(settings)
    ]

    # Core's claims plus every enabled feature's; an unclaimed surface is token-unreachable
    # (deny-by-default). Auth-exempt prefixes are composed the same way.
    scope_table = ScopeTable(
        [*CORE_CLAIMS, *(claim for m in enabled_manifests for claim in m.api_scopes)]
    )
    state.api_scope_table = scope_table
    public_prefixes = tuple(prefix for m in enabled_manifests for prefix in m.public_prefixes)

    # The agent's tool catalog, assembled once: core categories plus every enabled manifest's
    # toolsets, gated names and instruction providers. Chat turns, scheduled tasks and the
    # catalog routes share these objects, so the listing and the agent's stack can't diverge.
    tool_categories = core_categories()
    # Core categories have no manifest to declare gated names, so they seed the set. A tool
    # missing here (e.g. `shell`, which raises `ApprovalRequired` mid-call) is missing from
    # the scope vocabulary, so it could never be granted for a conversation and would ask
    # on every command.
    gated_tools: set[str] = set(CORE_GATED_TOOLS)
    # Tools that can't work offline; each feature declares its own, offline mode enforces the
    # union.
    network_tools: set[str] = set()
    # Categories that start dormant: schemas withheld until the model asks for the group by name.
    dormant_categories: list[DormantCategory] = []
    # (category, check) pairs for categories whose backing service the operator may not have
    # set up, resolved to tool names once every toolset is in hand. A feature answers for
    # every category it registers, since availability belongs to the service.
    availability_claims: list[tuple[str, AvailabilityCheck]] = []
    instruction_providers: list[InstructionProvider] = []
    # The `tasks` and `files` categories are core, so their tail context is seeded here.
    prompt_context_providers: list[PromptContextProvider] = [tasks_context, workspace_context]
    for manifest in enabled_manifests:
        # A built-in plugin's tools and prompt blocks go quiet while it is switched off.
        plugin = manifest.name if manifest.plugin is not None else None
        for category, factory in manifest.toolsets:
            if category in tool_categories:
                raise RuntimeError(
                    f"tool category {category!r} declared twice "
                    + f"(second claim by manifest {manifest.name!r})"
                )
            tool_categories[category] = factory()
            if plugin is not None:
                availability_claims.append(
                    (category, gate_availability(plugin, manifest.available))
                )
            elif manifest.available is not None:
                availability_claims.append((category, manifest.available))
        gated_tools |= manifest.gated_tools
        network_tools |= manifest.network_tools
        dormant_categories.extend(manifest.dormant)
        instruction_providers.extend(
            gate_instruction(plugin, p) if plugin is not None else p for p in manifest.instructions
        )
        prompt_context_providers.extend(
            gate_prompt_context(plugin, p) if plugin is not None else p
            for p in manifest.prompt_context
        )
    # A dormant name with no category would advertise a nonexistent group in the agent's index.
    for entry in dormant_categories:
        if entry.category not in tool_categories:
            raise RuntimeError(
                f"dormant category {entry.category!r} is not a registered tool category"
            )
    # An availability check with no toolset behind it would silently read as always available.
    for manifest in enabled_manifests:
        if manifest.available is not None and not manifest.toolsets:
            raise RuntimeError(
                f"manifest {manifest.name!r} declares an availability check "
                + "but registers no tool category"
            )
    # Namespaced to match the disabled set (`services/tool_policy.py`).
    bare_names = category_names(tool_categories)
    category_availability = tuple(
        CategoryAvailability(
            category=category,
            tools=frozenset(f"{category}_{name}" for name in bare_names[category]),
            check=check,
        )
        for category, check in availability_claims
    )
    state.tool_categories = tool_categories
    state.gated_tools = frozenset(gated_tools)
    state.category_availability = category_availability
    state.dormant_categories = tuple(dormant_categories)
    # Derived once: `routes/` cannot reach `harness/` to spell it, and per-composer
    # comprehensions drift apart.
    state.dormant_summaries = DormantCategory.summaries(dormant_categories)
    state.network_tools = frozenset(network_tools)
    state.instruction_providers = tuple(instruction_providers)
    state.prompt_context_providers = tuple(prompt_context_providers)

    install_error_handlers(app)

    # Added first ⇒ inner: auth runs inside CORS, so CORS answers preflight and decorates
    # even a 401.
    app.add_middleware(
        AuthMiddleware, scope_table=scope_table, extra_public_prefixes=public_prefixes
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(onboarding.router)
    app.include_router(runs.router)
    app.include_router(chat.router)
    app.include_router(conversations.router)
    app.include_router(overview.router)
    app.include_router(models.router)
    app.include_router(api_tokens.router)
    app.include_router(vm.router)
    app.include_router(tools.router)
    app.include_router(tokens.router)
    # A manifest whose `enabled` gate is off contributes nothing, so its routes 404; a
    # built-in plugin's routes also 404 while the operator has it switched off.
    for manifest in enabled_manifests:
        gate = [Depends(plugin_gate(manifest.name))] if manifest.plugin is not None else []
        for router in manifest.routers:
            app.include_router(router, dependencies=gate)
    return app


app = create_app()
