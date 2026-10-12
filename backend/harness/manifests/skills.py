"""The skill library (`SKILL-*`) — Agent Skills bundles, sealed at rest.

Deliberately *not* a corpus source — a skill is guidance to apply, not knowledge to
retrieve; it reaches the model through the per-turn catalog + `skills_open`.
"""

from __future__ import annotations

from core.api_scopes import ScopeClaim
from harness.manifest import FeatureManifest, FeatureRuntime, HarnessContext
from routes import skills as skills_routes
from services.revisions import RevisionStore
from services.skills import SkillStore
from tools.skills import skill_catalog_instructions, skills_toolset


async def _build(ctx: HarnessContext) -> FeatureRuntime:
    skills = SkillStore(ctx.engine, ctx.vault, RevisionStore(ctx.engine, ctx.vault))
    return FeatureRuntime(services=(skills,), capabilities=(skills,), state={"skills": skills})


MANIFEST = FeatureManifest(
    name="skills",
    routers=(skills_routes.router,),
    api_scopes=(ScopeClaim("knowledge", ("/skills",)),),
    toolsets=(("skills", skills_toolset),),
    # Every turn surfaces the published-skill catalog (names + descriptions only).
    instructions=(skill_catalog_instructions,),
    build=_build,
)
