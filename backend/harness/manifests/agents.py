"""Agents — custom agents the operator writes, and the catalog every thread and launch
resolves an agent through (built-ins, the project's files, stored agents)."""

from __future__ import annotations

from core.api_scopes import ScopeClaim
from harness.manifest import DormantCategory, FeatureManifest, FeatureRuntime, HarnessContext
from routes import agent_models as agent_models_routes
from routes import agents as agents_routes
from services.agent_models import AgentModels
from services.agents import AgentCatalog, AgentStore
from services.revisions import RevisionStore
from services.settings_store import SettingsStore
from tools.agents import GATED_TOOLS, agents_toolset


async def _build(ctx: HarnessContext) -> FeatureRuntime:
    store = AgentStore(ctx.engine, ctx.vault, RevisionStore(ctx.engine, ctx.vault))
    models = AgentModels(ctx.services.get(SettingsStore))
    catalog = AgentCatalog(store, models)
    return FeatureRuntime(
        services=(store, catalog, models),
        capabilities=(store, catalog, models),
        state={"agents": catalog, "agent_store": store, "agent_models": models},
    )


MANIFEST = FeatureManifest(
    name="agents",
    routers=(agents_routes.router, agent_models_routes.router),
    # Beside skills: both are instructions the operator writes for the model to follow.
    api_scopes=(ScopeClaim("knowledge", ("/agents",)),),
    toolsets=(("agents", agents_toolset),),
    gated_tools=GATED_TOOLS,
    dormant=(
        DormantCategory(
            "agents",
            "spawn agents to do self-contained work on their own and report back, message "
            + "the agents of your delegation tree, or draft a new agent for the operator to "
            + "publish",
        ),
    ),
    build=_build,
)
