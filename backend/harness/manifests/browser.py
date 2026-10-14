"""The browser-control feature — the agent drives a real page in the platform VM.

The browser is web fetch's Chromium (``services/browser/vm.py``), so this feature builds
after ``web`` and owns only the per-conversation sessions attached to it, the shared login
profile, and the operator's live view. Nothing starts here but the reaper: a session
attaches on the first browse tool call or the operator's own "open browser".
"""

from __future__ import annotations

from core.api_scopes import ScopeClaim
from harness.manifest import (
    DormantCategory,
    FeatureManifest,
    FeatureRuntime,
    HarnessContext,
)
from routes import browser as browser_routes
from services.browser import BrowserSessionManager, VMBrowser
from services.webfetch import ManagedBrowser
from tools.browse import NETWORK_TOOLS, browse_instructions, browse_toolset


async def _build(ctx: HarnessContext) -> FeatureRuntime:
    settings = ctx.settings
    browser = VMBrowser(
        ctx.services.get(ManagedBrowser),
        launch_timeout_s=settings.browser_control_launch_timeout_s,
    )
    sessions = BrowserSessionManager(
        browser,
        idle_ttl_s=settings.browser_control_idle_ttl_s,
        reap_interval_s=settings.browser_control_reap_interval_s,
        max_live=settings.browser_control_max_live,
        state_dir=settings.browser_control_state_dir,
    )
    await ctx.lifecycle.start("browser-sessions", start=sessions.start, stop=sessions.stop)
    return FeatureRuntime(
        services=(sessions,),
        capabilities=(sessions,),
        state={"browser_sessions": sessions},
    )


MANIFEST = FeatureManifest(
    name="browser",
    after=("web",),
    routers=(browser_routes.router,),
    # A thread's browser — opening it, watching it, driving it — is part of working in
    # that thread.
    api_scopes=(ScopeClaim("chat", ("/browser/session",)),),
    # No kill-switch of its own, deliberately. A switch that withheld the *category* would
    # make the operator's tool catalog disagree with the agent's real stack — the exact
    # divergence the namespacing exists to prevent. When no browser is available, the
    # tools assemble and degrade, like every other capability here.
    toolsets=(("browse", browse_toolset),),
    # The brief for those eighteen tools, and it belongs at the *head* rather than in
    # `prompt_context` even though it comes and goes: it is byte-stable for as long as the
    # session lives, and a stable block costs nothing in the head once the request that
    # first carried it is cached, where in the tail it would create a fresh divergence on
    # every later turn. It also switches on at the moment the reveal changes the tool
    # array, so it arrives inside an invalidation that has already been paid for.
    instructions=(browse_instructions,),
    # By far the most expensive category in the catalog, and the one the average turn
    # never opens — eighteen tools whose schemas would otherwise ride in every request
    # of every conversation, whether or not a page is ever loaded.
    dormant=(
        DormantCategory(
            "browse",
            "drive a real browser — navigate, click, type, read pages behind logins, "
            + "inspect network and console",
        ),
    ),
    network_tools=NETWORK_TOOLS,
    build=_build,
)
