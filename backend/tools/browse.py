"""The `browse` category — the agent drives a real page instead of only reading one.

`pydantic_ai_harness`'s `PlaywrightBrowserToolset`: navigate, read the accessibility tree,
click, type, press keys, choose options, hover, wait, scroll, go back and forward, run
JavaScript, manage tabs, answer dialogs, and read the page's console and network log.

**This is not `web_fetch`, and the difference is what it is for.** Fetch renders a URL and
hands back its text — one page, no state, nothing to click. These tools keep a browser
open *across* calls and across turns, which is what a login, a multi-step form, or an app
that only exists after JavaScript runs actually needs.

**Using the toolset alone meant the model was never told any of that**
(:func:`browse_instructions`). `PlaywrightBrowser` is two things — a per-run browser
lifecycle we replace, and the when-to-use-it guidance that comes with the tools — and
taking the toolset by itself
quietly left the second behind. The model got eighteen tool schemas and no account of how
they fit together: which tool reads a page, that `aria-ref` handles come from `snapshot`,
what to do about an iframe or a spinner, that a second tab exists. Guessing at all of that
is what a browsing turn looked like, and it read as the model being bad at browsing rather
than as never having been told.

**The session is the conversation's, not the run's.** `PlaywrightBrowser` (the capability)
opens a browser when a run starts and closes it when the run ends; that lifecycle is
bypassed here in favour of `services/browser`'s conversation-scoped manager, so the page
the operator is looking at when a turn finishes is the page the next turn continues on.
What is used from the harness is the toolset alone — the eighteen tools over a session we
own. See `tools/rebound.py` for why the tools are *defined* by a template and *dispatched*
through a per-conversation instance.

**The browser is the operator's too.** These tools drive a Chromium in the platform VM,
one browser context per conversation, which the operator watches and drives through the
app's live view — to log in before handing the thread over, or to do a step faster than
describing it. Nothing announces what they did; the model simply re-reads the page with
`snapshot` or `get_text` like it would after any other change.

**Degrades, never fails.** When the VM's browser is not up (no VM, offline mode, web fetch
disabled) there is nothing to attach to; the tools say so and the model moves on, exactly
as web fetch does under the same conditions.

**And the operator can end it at any moment, which is not the same failure.** A browser
they ended is a thing they chose, so the tools report *that* — the page is gone, the
logins are not, and they may simply be done — instead of starting a replacement mid-turn.
Only `navigate` reopens (:data:`REOPENING_TOOLS`), because a navigation is the model
deliberately starting somewhere; a `click` that silently opened a fresh blank page would
let it believe the click landed. An *idle* reap is ours rather than theirs and stays
invisible: the next call re-attaches with the profile restored.
"""

from __future__ import annotations

from pydantic_ai import AbstractToolset, RunContext
from pydantic_ai_harness.playwright import (
    EgressPolicy,
    PlaywrightBrowser,
    PlaywrightBrowserSession,
    PlaywrightBrowserToolset,
)

from prompts.browse import BROWSER_ADDENDUM
from services.browser import BrowserSessionManager

from .deps import RunDeps
from .rebound import ReboundToolset

#: The tools the category contributes, unprefixed — the harness's own set. Written out so
#: the namespaced names can be declared for offline mode and the vision gate without
#: constructing a toolset at import time; `tests/test_browse_tools.py` checks the literal
#: against the real toolset, so a harness rename fails there rather than silently
#: un-gating a tool.
TOOL_NAMES = frozenset(
    {
        "navigate",
        "snapshot",
        "click",
        "type_text",
        "press_key",
        "select_option",
        "hover",
        "wait_for",
        "screenshot",
        "get_text",
        "scroll",
        "go_back",
        "go_forward",
        "execute_js",
        "tabs",
        "handle_next_dialog",
        "console_messages",
        "network_requests",
    }
)

#: Every browse tool *is* the internet — there is no page to drive without it — so offline
#: mode withholds them all rather than offering the model tools that can only fail.
NETWORK_TOOLS = frozenset(f"browse_{name}" for name in TOOL_NAMES)

#: `screenshot` returns the image as `BinaryContent`, which only a vision model can read.
#: Declared here (the category that owns the tool) and applied by `services/tool_policy`.
VISION_TOOLS = frozenset({"browse_screenshot"})

_UNAVAILABLE = (
    "The browser is not available right now — the platform VM's browser is not running. "
    "Use web_fetch or web_search to read a page instead, or try again later."
)

#: Said when the *operator* ended the browser rather than when none could be opened. It
#: names what is gone (the page), what is not (the logins), and the one call that starts
#: over — so the model's next move is a decision rather than a retry.
_CLOSED = (
    "The operator ended this conversation's browser, so the page you were working on is "
    "gone along with anything unsaved on it. This is something they did, not a failure to "
    "retry: they may well be finished with it. If the work still needs a browser, say what "
    "you were in the middle of and call navigate to start again — the saved logins carry "
    "over, the page you were on does not. If it does not, read what you need with "
    "web_fetch or web_search, or ask them how they want to go on."
)

#: The one tool allowed to start a browser after the operator ended one. A navigation is
#: the model explicitly starting somewhere; every other tool acts on a page that no longer
#: exists, so reopening for those would hand it a blank page and let it believe its click
#: landed.
REOPENING_TOOLS = frozenset({"navigate"})


class BrowserToolset(ReboundToolset):
    """The harness browser tools, dispatched through the conversation's live session."""

    async def bind(self, name: str, ctx: RunContext[RunDeps]) -> AbstractToolset[RunDeps] | str:
        sessions = ctx.deps.caps.get_optional(BrowserSessionManager)
        if sessions is None:
            return _UNAVAILABLE
        key = ctx.deps.workspace_key
        live = await sessions.acquire(key, reopen=name in REOPENING_TOOLS)
        if live is None:
            return _CLOSED if sessions.closed(key) else _UNAVAILABLE
        # A reaped conversation can be re-attached under a *new* session; the cached
        # toolset would still be pointed at the dead one, so it is keyed by the session's
        # token rather than by the conversation.
        return self.cached(live.token, lambda: _toolset_for(live.session))


def _toolset_for(session: PlaywrightBrowserSession) -> AbstractToolset[RunDeps]:
    return PlaywrightBrowserToolset[RunDeps](session=session)


def browse_toolset() -> AbstractToolset[RunDeps]:
    """The `browse` category, built once at app assembly and shared by every run.

    The template's session is never entered, so constructing it starts no browser and
    needs no endpoint — it exists only to answer `get_tools` with the same definitions
    every run is offered.
    """
    template = PlaywrightBrowserSession(policy=_policy())
    return BrowserToolset("browse", _toolset_for(template))


def _policy() -> EgressPolicy:
    """The one policy shape both the real sessions and the instruction text are built from.

    Unenforced by design — the VM's platform proxy is the egress boundary, and
    `services/browser/session.py` argues why Playwright routing is not usable here. It is
    spelled once because the instructions *quote* it (`Allowed domains: …`), and a
    description of a boundary that came from a different object than the boundary is the
    kind of wrong nobody notices.
    """
    return EgressPolicy(block_private_addresses=False)


async def browse_instructions(ctx: RunContext[RunDeps]) -> str:
    """How to use the browser, for a conversation that currently has one open.

    **Borrowed from the harness rather than restated.** `PlaywrightBrowser` carries the
    guidance for its own eighteen tools and is maintained alongside them; constructing one
    starts no browser, so asking it for that text costs nothing and
    cannot drift from the tools actually offered. Ours is appended
    (:data:`prompts.browse.BROWSER_ADDENDUM`) and covers only what a general browser
    capability cannot know: that the operator watches and drives it, that the session
    outlives the turn, and that either of them can end it.

    **Silent until a browser is open, which is the whole reason this is affordable.** The
    category is dormant precisely because eighteen schemas on every request of every
    conversation is the most expensive thing in the catalog; a page of prose about them,
    shipped to every thread that never browses, would undo that. A live session is the
    cheapest honest signal that this thread is browsing — no I/O, no lookup — and it is
    stable for as long as the browsing lasts, so the head of the request stays byte-stable
    across the turns that matter for the prompt cache.

    The first `navigate` of a thread therefore runs uninstructed, and that is the accepted
    cost: the dormant index already says what the category is for, the model has just read
    the tool schemas it asked to be shown, and every call after the browser is open has the
    full text in front of it.
    """
    sessions = ctx.deps.caps.get_optional(BrowserSessionManager)
    if sessions is None or sessions.existing(ctx.deps.workspace_key) is None:
        return ""
    harness = PlaywrightBrowser(policy=_policy()).get_instructions()(ctx)
    return f"{harness}\n\n{BROWSER_ADDENDUM}" if harness else BROWSER_ADDENDUM
