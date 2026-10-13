"""Slash commands — the composer's `/` menu, and the workflows the operator saves for it.

Routes, the registry the chat route resolves a picked command through, and the one store
behind the only source the registry owns. There is no toolset and no instruction provider,
deliberately: a command is something the *operator* reaches for, and the model already has
every capability behind one. Telling it that a second way to ask exists would cost every
request a paragraph and change nothing about what it can do.

That last point is worth stating rather than inferring, because a workflow looks like
something the model should know about. It is not: it is a phrase the operator has bound to a
name for their own typing, and an agent that could read the list would be reading their
shortcuts. The tail block a command produces is the only thing about it the model ever sees.

The registry reads the skill library and the agent catalog, so this manifest builds after
both.
"""

from __future__ import annotations

from core.api_scopes import ScopeClaim
from harness.manifest import FeatureManifest, FeatureRuntime, HarnessContext
from routes import commands as command_routes
from services.agents import AgentCatalog
from services.commands import CommandRegistry
from services.commands.store import CommandStore
from services.skills import SkillStore


async def _build(ctx: HarnessContext) -> FeatureRuntime:
    store = CommandStore(ctx.engine, ctx.vault)
    registry = CommandRegistry(ctx.services.get(SkillStore), store, ctx.services.get(AgentCatalog))
    # Both on `app.state`: the registry is what a composer and a turn read, the store is
    # what the settings pane writes. Separate handles because they are separate audiences —
    # nothing that offers a command should be able to edit one.
    return FeatureRuntime(state={"commands": registry, "command_store": store}, services=(store,))


MANIFEST = FeatureManifest(
    name="commands",
    routers=(command_routes.router,),
    # Filed under `knowledge` beside `/skills`: the catalog is a listing of the operator's
    # own library seen from another angle, and a token allowed one has no reason to be
    # refused the other.
    #
    # The **write** half of that claim is the part worth deciding rather than inheriting,
    # because a token that can author a workflow can plant a template that later rides the
    # operator's own turns. It is granted anyway, and the reason is symmetry with what it
    # sits beside: `knowledge` already carries full authorship of *skills*, which are
    # strictly more powerful — the model chooses and opens those on its own, where a
    # workflow does nothing until the operator types its name. A narrower claim here would
    # withhold the weaker of the two capabilities while granting the stronger.
    api_scopes=(ScopeClaim("knowledge", ("/commands",)),),
    after=("agents", "skills"),
    build=_build,
)
