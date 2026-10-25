"""Plugins — tools the agent writes in Python, run in a VM of their own once the operator
publishes them."""

from __future__ import annotations

from core.api_scopes import ScopeClaim
from harness.manifest import DormantCategory, FeatureManifest, FeatureRuntime, HarnessContext
from routes import plugin_bridge as plugin_bridge_routes
from routes import plugin_ui as plugin_ui_routes
from routes import plugin_windows as plugin_windows_routes
from routes import plugins as plugins_routes
from routes import plugins_builtin as plugins_builtin_routes
from services.agents import AgentCatalog
from services.plugins import PluginRuntime, PluginSecretStore, PluginStore
from services.plugins.bridge.gate import BridgeGate
from services.plugins.storage import PluginStorage
from services.plugins.windows import WindowSessions
from services.revisions import RevisionStore
from services.settings_store import SettingsStore
from services.vm import ConversationMachines
from services.vm.target import machine_prefix
from tools.plugins import GATED_TOOLS, plugins_toolset


async def _build(ctx: HarnessContext) -> FeatureRuntime:
    store = PluginStore(ctx.engine, ctx.vault, RevisionStore(ctx.engine, ctx.vault))
    machines = ctx.services.get_optional(ConversationMachines) if ctx.settings.vm_enabled else None
    runtime = (
        None
        if machines is None
        else PluginRuntime(
            machines.platform,
            ctx.settings.data_dir / "plugins" / "installed",
            prefix=machine_prefix(ctx.settings.data_dir),
        )
    )
    secrets = PluginSecretStore(ctx.engine, ctx.vault, store)
    windows = WindowSessions()
    store.on_change(windows.revoke)
    ctx.vault.register_on_lock(windows.clear)
    bridge = (
        windows,
        PluginStorage(ctx.engine, ctx.vault),
        BridgeGate(ctx.services.get(SettingsStore)),
    )
    ctx.services.get(AgentCatalog).contribute(store.agents)
    services = (store, secrets) if runtime is None else (store, secrets, runtime)
    return FeatureRuntime(
        services=(*services, *bridge), capabilities=services, state={"plugins": store}
    )


MANIFEST = FeatureManifest(
    name="plugins",
    # Its published plugins' agents join the agent catalog.
    after=("agents",),
    # The fixed paths first: `/plugins/{id}` would otherwise claim `/plugins/builtin` and
    # `/plugins/windows`.
    routers=(
        plugins_builtin_routes.router,
        plugin_windows_routes.router,
        plugin_bridge_routes.router,
        plugins_routes.router,
        plugin_ui_routes.router,
    ),
    api_scopes=(ScopeClaim("knowledge", ("/plugins",)),),
    # A window's page token is its credential; the page has no session cookie to send.
    public_prefixes=("/plugin-ui",),
    toolsets=(("plugins", plugins_toolset),),
    gated_tools=GATED_TOOLS,
    dormant=(
        DormantCategory(
            "plugins",
            "the tools of the operator's published plugins, and writing and testing new "
            + "plugins: tools in Python that each run in a VM of their own, and small apps "
            + "and games that open in a floating window",
        ),
    ),
    build=_build,
)
