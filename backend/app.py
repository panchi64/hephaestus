"""FastAPI application assembly: middleware, auth, routers and the singletons on ``app.state``.

Business logic lives below this layer. See ``docs/architecture/README.md``.
"""

from __future__ import annotations

import logging
import sys
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path
from typing import cast

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import Engine

from agent.work_summary import summarize_work
from core.api_scopes import CORE_CLAIMS, ScopeTable
from core.auth import AuthManager, AuthMiddleware
from core.config import Settings, get_settings
from core.db import init_db, make_engine
from core.devserver import UNGUARDED_RELOAD_WARNING, reload_watches_runtime_state
from core.http_errors import install_error_handlers
from core.shutdown import begin_shutdown, reset_shutdown
from core.vault import Vault
from harness import LifecycleRegistry
from harness.discovery import discover_manifests
from harness.manifest import DormantCategory, HarnessContext, ServiceContainer
from harness.run_terminal import RunTerminalDispatcher
from models.artifact import Artifact
from models.conversation import Conversation
from models.corpus import CorpusSource
from routes import (
    api_tokens,
    auth,
    chat,
    conversations,
    health,
    models,
    overview,
    runs,
    tokens,
    tools,
    vm,
)
from routes.deps import OPERATOR_ID, AppState, app_state
from runs import LaneLimits, PrefixLedger, RunRegistry
from services.api_token_store import ApiTokenStore
from services.approval_grants import ApprovalGrantStore
from services.approval_ledger import ApprovalLedger
from services.conversations import ConversationStore
from services.credential_store import CredentialStore
from services.egress import EgressPolicy
from services.embeddings import RegistryEmbedder
from services.home.scratch import ScratchDirs
from services.plan_mode import PlanMode
from services.registry import ModelRegistry
from services.sandbox import (
    resolve_confinement,
    shutdown_confinement,
    stop_all_groups,
)
from services.sealing import seal_legacy_column
from services.settings_store import SettingsStore, TurnTuning
from services.task_list import ConversationTasks
from services.tool_policy import AvailabilityCheck, CategoryAvailability
from services.vm import ConversationMachines, GuestImage, Platform, Previews, ServicesMachine
from services.vm.target import machine_prefix
from services.work_summaries import WorkSummaryService
from services.worktree_switch import WorktreeSwitch
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


async def backfill_sealed_columns(engine: Engine, vault: Vault) -> None:
    """After unlock, seal the columns once stored in the clear and drop the cleartext.

    A migration can't: schema upgrades run with the vault locked, so there is no key.
    Idempotent (a healed row no longer matches) and best-effort per column, so one failure
    neither blocks the others nor takes the boot down."""
    _ = await vault.unlocked_event.wait()
    for model_cls, legacy, sealed in (
        (Conversation, "title", "title_enc"),
        (CorpusSource, "path", "path_enc"),
        (Artifact, "title", "title_enc"),
        (Artifact, "filename", "filename_enc"),
    ):
        try:
            _ = await seal_legacy_column(
                engine=engine,
                vault=vault,
                model_cls=model_cls,
                legacy_attr=legacy,
                sealed_attr=sealed,
            )
        except Exception:
            logger.exception("at-rest: sealing legacy %s values failed", legacy)


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
    """Build, wire, and start everything the app owns, in dependency order.

    Every unit needing shutdown registers on ``lifecycle`` at its construction point, so
    one ``stop_all`` unwinds even a partially built graph.
    """
    state = app_state(app)
    # Typed capability handles. Core wiring adds what it builds; each manifest's build
    # resolves its cross-feature dependencies here, never by importing another feature's wiring.
    container = ServiceContainer()
    state.services = container
    # The agent-facing bag (`RunDeps.caps`): the curated services the agent's tools may
    # reach, from each manifest's `capabilities` export plus the core handles below. Every
    # run path hands this same bag to the engine.
    agent_capabilities = ServiceContainer()
    state.capabilities = agent_capabilities
    state.auth_manager = AuthManager()
    # Inbound scoped API tokens. Named here because the auth gate reads it off `app.state`
    # on every request, including ones before the lifespan finishes; None means no second
    # authentication method yet.
    state.api_tokens = None

    # The registry's terminal-transition point: features add hooks (waiter resolution,
    # notification policy) without `runs/` importing `services/`. The in-flight tasks stay on
    # `app.state.run_terminal_tasks` so shutdown drains them and tests can await them.
    run_terminal = RunTerminalDispatcher()
    state.run_terminal_tasks = run_terminal.tasks

    state.runs = RunRegistry(
        lanes=LaneLimits(
            interactive=settings.run_max_concurrency,
            routine=settings.run_routine_concurrency,
            background=settings.run_background_concurrency,
            linked=settings.run_linked_concurrency,
        ),
        wall_clock_timeout_s=settings.run_wall_clock_timeout_s,
        inactivity_timeout_s=settings.run_inactivity_timeout_s,
        on_terminal=run_terminal,
    )

    settings.data_dir.mkdir(parents=True, exist_ok=True)
    # `uvicorn app:app --reload` watches the data directory and restarts the server when
    # runtime state changes; the reloader is the parent process, so only a warning is possible.
    if reload_watches_runtime_state(sys.argv, settings.data_dir):
        logger.warning(UNGUARDED_RELOAD_WARNING, settings.data_dir)
    url = settings.db_url or f"sqlite:///{settings.data_dir / 'app.db'}"
    # Read before `init_db`, which creates the database and makes a deleted one look fresh.
    # The keyfile sits beside the database, so with a cleared `app.db` the operator would
    # still be asked to unlock a key protecting nothing; `/auth/status` reports both as
    # `db_missing`. An explicit `db_url` counts as intact (we don't own its path).
    db_path = None if settings.db_url else settings.data_dir / "app.db"
    state.workspace_db_intact = db_path.exists() if db_path else True
    engine = make_engine(url)
    init_db(engine)
    state.db_engine = engine
    # Shared by the auth gate and `/tokens`: two stores with separate verification caches
    # would let a revoked token stay trusted.
    state.api_tokens = ApiTokenStore(engine, state.api_scope_table)

    # The at-rest encryption vault. A passphrase sets it up or unlocks it at boot (the
    # auth-disabled path); otherwise it stays locked until login/setup.
    vault = Vault(settings.data_dir / "keyfile.json")
    state.vault = vault
    if settings.unlock_passphrase:
        if vault.is_initialized:
            _ = await vault.unlock(settings.unlock_passphrase)
        else:
            await vault.setup(settings.unlock_passphrase)

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
    registry = ModelRegistry(engine, vault, http_client=discovery_client, gate=state.runs.endpoints)
    state.models = registry
    # Shared by long-term memory and cross-chat search; degrades to keyword recall when no
    # embedding endpoint is set.
    embedder = RegistryEmbedder(registry)
    # In-memory working tree plus write-behind persistence. Embeds each persisted turn
    # (best-effort) for cross-chat search, so it is built after the registry for the embedder.
    state.conversations = ConversationStore(engine, vault, embedder)
    await lifecycle.start(
        "conversations",
        start=state.conversations.start,
        stop=state.conversations.stop,
    )
    # The operator's API keys for third-party services, sealed with the vault.
    state.credentials = CredentialStore(engine, vault)
    # Built before the capabilities that read it: the notification channels resolve their
    # configuration from it.
    state.settings_store = SettingsStore(engine)
    # Conversation-scoped tool auto-approval grants; core, beside the run substrate the
    # approvals park on.
    state.approval_grants = ApprovalGrantStore(engine, settings.approval_grant_ttl_s)
    state.approval_ledger = ApprovalLedger(engine, vault)
    # The grants remember which tools may run unasked, this which domains a conversation's
    # machine may reach.
    state.egress = EgressPolicy(engine, settings.egress_allowed_domains)
    # The sealing migration ran before unlock with no key, so healing happens here.
    _ = lifecycle.track("sealing-backfill", backfill_sealed_columns(engine, vault))
    # The image builds in the background; until it is ready, what needs a machine reports
    # unavailable rather than running on the host.
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
    container.add(platform)
    prefix = machine_prefix(settings.data_dir)
    services_machine = ServicesMachine(
        platform,
        name=f"{prefix}-services",
        cpus=settings.vm_services_cpus,
        memory_mb=settings.vm_services_memory_mb,
    )
    state.vm = services_machine
    await lifecycle.start(
        "services-machine", start=services_machine.start, stop=services_machine.stop
    )
    state.scratch = ScratchDirs(
        settings.data_dir / "sandbox" / "work", excludes=settings.sandbox_walk_excludes
    )
    machines = (
        ConversationMachines(
            platform,
            prefix=prefix,
            cpus=settings.sandbox_cpus,
            memory_mb=settings.sandbox_memory_mb,
            idle_ttl_s=settings.sandbox_session_idle_ttl_s,
            reap_interval_s=settings.sandbox_session_reap_interval_s,
            max_machines=settings.sandbox_max_sessions,
        )
        if settings.sandbox_enabled and settings.vm_enabled
        else None
    )
    state.machines = machines
    state.previews = None
    if machines is not None:
        await lifecycle.start("conversation-machines", start=machines.start, stop=machines.stop)
        state.previews = Previews(
            machines, startup_timeout_s=settings.sandbox_preview_startup_timeout_s
        )
    else:
        logger.info("sandbox: commands disabled")

    # A periodic sweep, so it follows the store and registry it reads from.
    state.work_summaries = WorkSummaryService(
        store=state.conversations,
        settings_store=state.settings_store,
        models=registry,
        runs=state.runs,
        vault=vault,
        settings=settings,
        owner_id=OPERATOR_ID,
        # Injected: `agent` sits above `services`, and only assembly may know both.
        summarize=summarize_work,
    )
    await lifecycle.start(
        "work-summaries",
        start=state.work_summaries.start,
        stop=state.work_summaries.stop,
    )

    # Handles still hand-wired above, registered so manifests can resolve them.
    for handle in (
        state.auth_manager,
        run_terminal,
        state.runs,
        state.models,
        embedder,
        state.conversations,
        state.credentials,
        state.settings_store,
        state.approval_grants,
        state.egress,
        state.api_tokens,
        services_machine,
        state.scratch,
    ):
        container.add(handle)
    agent_capabilities.add(state.scratch)
    if machines is not None:
        container.add(machines)
        agent_capabilities.add(machines)
    if state.previews is not None:
        agent_capabilities.add(state.previews)
    # Core-owned agent-facing handles: the engine consults grants at the deferred split,
    # and the egress tool and fences read the policy. The rest arrive via manifests.
    agent_capabilities.add(state.approval_grants)
    agent_capabilities.add(state.approval_ledger)
    agent_capabilities.add(state.egress)
    # Sub-agents resolve the `utility` model via the same `resolve_background` rule as
    # titling and verification.
    agent_capabilities.add(state.models)
    # The operator's first-token wait and step-limit overrun, read per turn by the engine.
    agent_capabilities.add(TurnTuning(state.settings_store))
    # Core-owned because `tasks` is a core category, not a manifest's.
    state.conversation_tasks = ConversationTasks(engine, vault)
    container.add(state.conversation_tasks)
    agent_capabilities.add(state.conversation_tasks)
    # Plan mode gets the store and task list so approving a plan can move the thread's
    # permission level and seed its tasks through this one seam. `ConversationStore` stays
    # out of the agent's bag: a tool that could read every thread is not needed.
    state.plan_mode = PlanMode(
        engine, vault, conversations=state.conversations, tasks=state.conversation_tasks
    )
    container.add(state.plan_mode)
    agent_capabilities.add(state.plan_mode)
    # Opening a worktree moves a thread's workspace, through the same narrow seam.
    agent_capabilities.add(WorktreeSwitch(state.conversations))
    # Last request fingerprint per conversation, so a turn's first request can say what moved
    # in the head. Absent means within-turn comparison only (`runs/prefix.py`).
    agent_capabilities.add(PrefixLedger())

    # Feature manifests build last, in `after` order, resolving from the core wired above.
    # A build's results wire in here: services, `app.state` names for `routes/deps.py`, and
    # run-terminal hooks. Their routers were registered in `create_app`.
    ctx = HarnessContext(
        settings=settings,
        engine=engine,
        vault=vault,
        lifecycle=lifecycle,
        services=container,
        capabilities=agent_capabilities,
        tool_categories=state.tool_categories,
        instruction_providers=state.instruction_providers,
        prompt_context_providers=state.prompt_context_providers,
        network_tools=state.network_tools,
        dormant_categories=state.dormant_categories,
        category_availability=state.category_availability,
        app=app,
    )
    for manifest in state.feature_manifests:
        if manifest.enabled is not None and not manifest.enabled(settings):
            continue
        if manifest.build is None:
            continue
        runtime = await manifest.build(ctx)
        for service in runtime.services:
            container.add(service)
        for capability in runtime.capabilities:
            # An `(instance, as_type)` pair keys by an abstraction, keeping a capability
            # above `tools/` reachable from it (`FeatureRuntime.capabilities`).
            if isinstance(capability, tuple):
                instance, as_type = cast(tuple[object, type[object]], capability)
                agent_capabilities.add(instance, as_type=as_type)
            else:
                agent_capabilities.add(capability)
        for name, value in runtime.state.items():
            setattr(state, name, value)
        for sync_hook in runtime.run_terminal_sync:
            run_terminal.add_sync(sync_hook)
        for hook in runtime.run_terminal:
            run_terminal.add(hook)
    ctx.built.set()

    # Registered last so it stops first: live runs write through the stores and sandbox, so
    # they must be cancelled (and flushed) while those still run. Otherwise a turn in flight
    # submits onto a stopped write-behind drainer, which discards it silently.
    lifecycle.on_stop("runs", partial(state.runs.shutdown, settings.shutdown_drain_timeout_s))


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
        for category, factory in manifest.toolsets:
            if category in tool_categories:
                raise RuntimeError(
                    f"tool category {category!r} declared twice "
                    + f"(second claim by manifest {manifest.name!r})"
                )
            tool_categories[category] = factory()
            if manifest.available is not None:
                availability_claims.append((category, manifest.available))
        gated_tools |= manifest.gated_tools
        network_tools |= manifest.network_tools
        dormant_categories.extend(manifest.dormant)
        instruction_providers.extend(manifest.instructions)
        prompt_context_providers.extend(manifest.prompt_context)
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
    app.include_router(runs.router)
    app.include_router(chat.router)
    app.include_router(conversations.router)
    app.include_router(overview.router)
    app.include_router(models.router)
    app.include_router(api_tokens.router)
    app.include_router(vm.router)
    app.include_router(tools.router)
    app.include_router(tokens.router)
    # A manifest whose `enabled` gate is off contributes nothing, so its routes 404.
    for manifest in enabled_manifests:
        for router in manifest.routers:
            app.include_router(router)
    return app


app = create_app()
