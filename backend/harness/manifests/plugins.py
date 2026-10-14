"""Plugins — tools the agent writes in Python, run in the platform VM once the operator
publishes them."""

from __future__ import annotations

from core.api_scopes import ScopeClaim
from harness.manifest import DormantCategory, FeatureManifest, FeatureRuntime, HarnessContext
from routes import plugins as plugins_routes
from services.agents import AgentCatalog
from services.plugins import PluginRuntime, PluginStore
from services.revisions import RevisionStore
from services.vm import PlatformVM
from tools.plugins import GATED_TOOLS, plugins_toolset


async def _build(ctx: HarnessContext) -> FeatureRuntime:
    store = PluginStore(ctx.engine, ctx.vault, RevisionStore(ctx.engine, ctx.vault))
    vm = ctx.services.get_optional(PlatformVM) if ctx.settings.vm_enabled else None
    runtime = (
        None if vm is None else PluginRuntime(vm, ctx.settings.data_dir / "plugins" / "egress")
    )
    ctx.services.get(AgentCatalog).contribute(store.agents)
    services = (store,) if runtime is None else (store, runtime)
    return FeatureRuntime(services=services, capabilities=services, state={"plugins": store})


MANIFEST = FeatureManifest(
    name="plugins",
    # Its published plugins' agents join the agent catalog.
    after=("agents",),
    routers=(plugins_routes.router,),
    api_scopes=(ScopeClaim("knowledge", ("/plugins",)),),
    toolsets=(("plugins", plugins_toolset),),
    gated_tools=GATED_TOOLS,
    dormant=(
        DormantCategory(
            "plugins",
            "the tools of the operator's published plugins, and writing and testing new "
            + "plugins: tools in Python that run in the platform VM",
        ),
    ),
    build=_build,
)
