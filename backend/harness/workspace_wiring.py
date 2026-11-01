"""Everything that reads or writes the operator's data, built only while the workspace is open.

`harness.workspace.WorkspaceSession` runs :func:`wire_workspace` at unlock, after the volume
is mounted and the encrypted engine is migrated, and tears it all down at lock through the
session's own lifecycle.
"""

from __future__ import annotations

import logging
from functools import partial
from typing import cast

import httpx
from fastapi import FastAPI
from sqlalchemy import Engine

from agent.work_summary import summarize_work
from core.config import Settings
from core.vault import Vault
from harness import LifecycleRegistry
from harness.builtin_plugins import BuiltinPlugins
from harness.manifest import HarnessContext, ServiceContainer
from harness.run_terminal import RunTerminalDispatcher
from models.artifact import Artifact
from models.conversation import Conversation
from models.corpus import CorpusSource
from routes.deps import OPERATOR_ID, app_state
from runs import LaneLimits, PrefixLedger, RunRegistry
from services.api_token_store import ApiTokenStore
from services.approval_grants import ApprovalGrantStore
from services.conversations import ConversationStore
from services.credential_store import CredentialStore
from services.egress import EgressPolicy
from services.embeddings import RegistryEmbedder
from services.home.scratch import ScratchDirs
from services.plan_mode import PlanMode
from services.private_area import PrivateArea
from services.registry import ModelRegistry
from services.sealing import seal_legacy_column
from services.settings_store import SettingsStore, TurnTuning
from services.task_list import ConversationTasks
from services.vm import ConversationMachines, Platform, Previews, ServicesMachine
from services.vm.target import machine_prefix
from services.work_summaries import WorkSummaryService
from services.worktree_switch import WorktreeSwitch

logger = logging.getLogger(__name__)


async def backfill_sealed_columns(engine: Engine, vault: Vault) -> None:
    """Seal the columns once stored in the clear and drop the cleartext.

    Idempotent (a healed row no longer matches) and best-effort per column, so one failure
    neither blocks the others nor takes the open down."""
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


async def wire_workspace(
    app: FastAPI,
    settings: Settings,
    lifecycle: LifecycleRegistry,
    *,
    engine: Engine,
    vault: Vault,
    platform: Platform,
    discovery_client: httpx.AsyncClient,
) -> None:
    """Build, wire, and start every unit over the open database, in dependency order.

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

    # The registry's terminal-transition point: features add hooks (waiter resolution,
    # notification policy) without `runs/` importing `services/`. The in-flight tasks stay on
    # `app.state.run_terminal_tasks` so shutdown drains them and tests can await them.
    run_terminal = RunTerminalDispatcher()
    state.run_terminal_tasks = run_terminal.tasks
    # Per session: a registry that has shut down refuses new runs for good.
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

    state.db_engine = engine
    # Shared by the auth gate and `/tokens`: two stores with separate verification caches
    # would let a revoked token stay trusted.
    state.api_tokens = ApiTokenStore(engine, state.api_scope_table)
    lifecycle.on_stop("api-tokens", partial(setattr, state, "api_tokens", None))

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
    state.private_area = PrivateArea(vault, state.settings_store, state.conversations)
    vault.register_on_lock(state.private_area.lock)
    # Conversation-scoped tool auto-approval grants; core, beside the run substrate the
    # approvals park on.
    state.approval_grants = ApprovalGrantStore(engine, settings.approval_grant_ttl_s)
    # The grants remember which tools may run unasked, this which domains a conversation's
    # machine may reach.
    state.egress = EgressPolicy(engine, settings.egress_allowed_domains)
    _ = lifecycle.track("sealing-backfill", backfill_sealed_columns(engine, vault))
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
    state.scratch = ScratchDirs(settings.scratch_dir, excludes=settings.sandbox_walk_excludes)
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
        state.private_area,
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
    agent_capabilities.add(state.egress)
    # Recall and memory tools keep private threads out unless the area is open and the
    # operator approves.
    agent_capabilities.add(state.private_area)
    # `agents_models` and a spawn's `model` resolve the operator's choosable list here.
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
    enabled_manifests = [
        m for m in state.feature_manifests if m.enabled is None or m.enabled(settings)
    ]
    builtin_plugins = BuiltinPlugins(state.settings_store, enabled_manifests)
    await builtin_plugins.load()
    container.add(builtin_plugins)
    agent_capabilities.add(builtin_plugins)
    for manifest in enabled_manifests:
        if manifest.build is None:
            continue
        runtime = await manifest.build(ctx)
        if manifest.plugin is not None:
            await builtin_plugins.attach(manifest.name, runtime.contributions)
            # At the build's position, so a plugin stands down in the order it came up.
            lifecycle.on_stop(
                f"plugin-{manifest.name}", partial(builtin_plugins.deactivate, manifest.name)
            )
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
