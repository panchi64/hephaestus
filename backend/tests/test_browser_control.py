"""Browser control: the conversation-scoped session and the tools bound to it.

Two layers, fastest-first:
- ``BrowserSessionManager`` — attach/reap/evict, the degrade path, and what counts as
  somebody using a browser (fake host, fake sessions).
- the toolset — that two conversations drive two *pages*, not merely two toolsets.

Nothing here starts Chromium: what is worth guarding is the wiring around the harness's
browser, and launching a real one would make these tests slow, flaky and
environment-dependent without testing more of our own code.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import cast, override

import pytest
from playwright.async_api import Browser
from pydantic_ai import RunContext
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage
from pydantic_ai_harness.playwright import PlaywrightBrowserSession, PlaywrightBrowserToolset

from core.container import ServiceContainer
from runs import Run, RunStream
from services.browser import BrowserSessionManager, ControlledBrowserSession, LiveBrowser
from services.browser import session as session_module
from services.browser.session import BrowserHost
from tools import RunDeps
from tools import browse as browse_module
from tools.browse import TOOL_NAMES, BrowserToolset, browse_instructions, browse_toolset

from ._json import obj, parse_json

OWNER = "operator"


# --- fakes -----------------------------------------------------------------------------


class _FakeContext:
    """The browser context behind a page: the tabs really open in it, and the cookie jar
    the manager saves into the profile."""

    def __init__(self, state: dict[str, object] | None = None) -> None:
        self.state: dict[str, object] = state or {"cookies": [{"name": "session", "value": "abc"}]}
        self.pages: list[_FakePage] = []

    async def storage_state(self) -> dict[str, object]:
        return self.state


class _FakePage:
    def __init__(
        self, url: str = "https://example.com", context: _FakeContext | None = None
    ) -> None:
        self.url: str = url
        self.context: _FakeContext = context or _FakeContext()
        self.context.pages.append(self)
        self.init_scripts: list[str] = []
        self.fronted: int = 0

    async def title(self) -> str:
        return "Example"

    async def add_init_script(self, script: str) -> None:
        self.init_scripts.append(script)

    async def bring_to_front(self) -> None:
        self.fronted += 1


class _FakeSession:
    """Stands in for a harness browser session: an active page, the open tabs, and the
    event counter the sweep reads activity off."""

    def __init__(self, page: _FakePage | None = None, /, **kwargs: object) -> None:
        self.page: _FakePage = page or _FakePage()
        self.pages: list[_FakePage] = [self.page]
        self.events_recorded: int = 0
        self.exited: bool = False
        self.kwargs: dict[str, object] = kwargs  # what `_attach` constructed this with

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *_args: object) -> None:
        self.exited = True

    async def ensure_page(self) -> _FakePage:
        return self.page


class _FakeHost(BrowserHost):
    """A browser host whose browser a test can take away."""

    def __init__(self, cdp_url: str | None = "http://127.0.0.1:9222") -> None:
        self.fake_cdp_url: str | None = cdp_url
        self.stealthed: list[object] = []

    @property
    @override
    def cdp_url(self) -> str | None:
        return self.fake_cdp_url

    @override
    async def ensure(self) -> str | None:
        return self.fake_cdp_url

    @override
    async def apply_stealth(self, page: object) -> None:
        self.stealthed.append(page)


def _manager(
    host: BrowserHost,
    *,
    idle_ttl_s: float = 900.0,
    reap_interval_s: float = 60.0,
    max_live: int = 3,
    state_dir: Path | None = None,
) -> BrowserSessionManager:
    return BrowserSessionManager(
        host,
        idle_ttl_s=idle_ttl_s,
        reap_interval_s=reap_interval_s,
        max_live=max_live,
        state_dir=state_dir,
    )


def _live(manager: BrowserSessionManager, key: str, page: _FakePage | None = None) -> LiveBrowser:
    """Install a fake session under ``key``, bypassing the real CDP attach."""
    session = _FakeSession(page)
    live = LiveBrowser(
        key,
        f"token-{key}",
        cast(ControlledBrowserSession, cast(object, session)),
        None,
        session.page.context,
    )
    manager.sessions[key] = live  # constructing the state under test
    return live


def _fake(live: LiveBrowser) -> _FakeSession:
    assert isinstance(live.session, _FakeSession)
    return live.session


def _context(live: LiveBrowser) -> _FakeContext:
    assert isinstance(live.context, _FakeContext)
    return live.context


def _browse() -> BrowserToolset:
    toolset = browse_toolset()
    assert isinstance(toolset, BrowserToolset)
    return toolset


@pytest.fixture
def attached(monkeypatch: pytest.MonkeyPatch) -> list[_FakeSession]:
    """Make the real attach path build fake sessions, newest last."""
    built: list[_FakeSession] = []

    def _build(**kwargs: object) -> _FakeSession:
        session = _FakeSession(**kwargs)
        built.append(session)
        return session

    monkeypatch.setattr(session_module, "ControlledBrowserSession", _build)
    return built


# --- the session manager ---------------------------------------------------------------


async def test_no_browser_to_attach_to_degrades_rather_than_raising():
    manager = _manager(_FakeHost(cdp_url=None))
    assert await manager.acquire("c1") is None


async def test_a_conversation_keeps_one_session_across_turns():
    manager = _manager(_FakeHost())
    live = _live(manager, "c1")
    # Two sequential turns: the second must find the first turn's browser, not open one.
    assert await manager.acquire("c1") is live
    assert await manager.acquire("c1") is live
    assert manager.existing("c1") is live


async def test_two_conversations_get_two_sessions():
    manager = _manager(_FakeHost())
    first, second = _live(manager, "c1"), _live(manager, "c2")
    assert first is not second
    assert _fake(first).page is not _fake(second).page


@pytest.mark.usefixtures("attached")
async def test_an_attached_page_is_stealthed_and_left_where_it_is():
    # The masking is what keeps the page from being served a challenge. Coming forward is
    # not part of attaching: the agent's first browse tool call attaches too, and that one
    # must not throw a window over whatever the operator is doing.
    host = _FakeHost()
    manager = _manager(host)

    live = await manager.acquire("c1")

    assert live is not None
    page = _fake(live).page
    assert host.stealthed == [page]
    assert page.fronted == 0


async def test_a_login_made_in_one_conversation_reaches_the_next(
    tmp_path: Path, attached: list[_FakeSession]
):
    # The reason the profile exists: the operator logs in once, and a later conversation's
    # agent must not land on a login page.
    manager = _manager(_FakeHost(), state_dir=tmp_path)
    _ = await manager.acquire("c1")

    await manager.release("c1")

    saved = parse_json((tmp_path / "profile.json").read_text())
    assert obj(saved)["cookies"] == [{"name": "session", "value": "abc"}]
    _ = await manager.acquire("c2")
    assert attached[-1].kwargs["storage_state"] == saved


@pytest.mark.usefixtures("attached")
async def test_deleting_a_conversation_keeps_the_shared_profile(tmp_path: Path):
    manager = _manager(_FakeHost(), state_dir=tmp_path)
    _ = await manager.acquire("c1")

    await manager.purge("c1")

    assert (tmp_path / "profile.json").exists()
    assert manager.existing("c1") is None
    await manager.purge("c1")  # idempotent: deleting twice is not an error


@pytest.mark.usefixtures("attached")
async def test_forgetting_the_profile_ends_every_session_unsaved(tmp_path: Path):
    manager = _manager(_FakeHost(), state_dir=tmp_path)
    _ = await manager.acquire("c1")
    await manager.release("c1")
    live = await manager.acquire("c2")

    await manager.forget_profile()

    assert not (tmp_path / "profile.json").exists()
    assert manager.existing("c2") is None
    assert live is not None and getattr(live.session, "exited", False)


@pytest.mark.usefixtures("attached")
async def test_a_session_whose_window_is_gone_is_not_handed_out_again():
    # The sweep is what normally notices, and it runs on its own clock — an operator who
    # quits Chromium and reopens their browser a second later must not be given the dead
    # session with a cheerful "active".
    host = _FakeHost()
    manager = _manager(host)
    stale = await manager.acquire("c1")
    assert stale is not None

    host.fake_cdp_url = None
    assert await manager.acquire("c1") is None  # nothing to attach to: degrade, not a lie
    assert _fake(stale).exited

    host.fake_cdp_url = "http://127.0.0.1:9222"
    fresh = await manager.acquire("c1")

    assert fresh is not None and fresh is not stale


async def test_an_idle_session_is_reaped():
    manager = _manager(_FakeHost(), idle_ttl_s=0.0)
    live = _live(manager, "c1")
    await manager.sweep()  # driving the reaper directly, no clock wait

    assert manager.existing("c1") is None
    assert _fake(live).exited


async def test_the_operators_own_browsing_counts_as_use():
    # The idle clock only moves on the agent's tool calls, so a browser the operator is
    # working in all afternoon looks perfectly idle — and would be reaped mid-login.
    manager = _manager(_FakeHost(), idle_ttl_s=0.0)
    events, navigation = _live(manager, "c1"), _live(manager, "c2")

    _fake(events).events_recorded += 1
    _fake(navigation).page.url = "https://example.com/billing"
    await manager.sweep()

    assert manager.existing("c1") is events and manager.existing("c2") is navigation

    await manager.sweep()  # nothing moved this time

    assert manager.existing("c1") is None and manager.existing("c2") is None


async def test_a_window_with_no_tabs_left_is_released():
    # Closing the last tab is as clear a "done with this" as an operator can give, and
    # there is no page left for the session to be about.
    manager = _manager(_FakeHost())
    live = _live(manager, "c1")
    _context(live).pages.clear()

    await manager.sweep()

    assert manager.existing("c1") is None
    assert _fake(live).exited


async def test_a_tab_the_operator_opened_keeps_the_session_alive():
    # The harness only tracks the tabs it opened, so an operator who opens one of their own
    # and closes the agent's would look like an empty window — and reaping the session
    # closes the context, taking their tab with it.
    manager = _manager(_FakeHost(), idle_ttl_s=0.0)
    live = _live(manager, "c1")
    _context(live).pages.clear()  # they closed the agent's tab
    theirs = _FakePage(url="https://example.com/inbox", context=_context(live))
    _fake(live).pages = []  # never held their tab
    _fake(live).page = theirs

    await manager.sweep()

    assert manager.existing("c1") is live

    # And what they do in it counts as use, the same way the agent's own tab does.
    theirs.url = "https://example.com/inbox/1"
    await manager.sweep()

    assert manager.existing("c1") is live


async def test_the_browser_going_away_reaps_every_session():
    # A VM reset or offline mode takes every session with it: `cdp_url` goes None and
    # nothing any of them holds is real any more.
    host = _FakeHost()
    manager = _manager(host)
    first, second = _live(manager, "c1"), _live(manager, "c2")

    host.fake_cdp_url = None
    await manager.sweep()

    assert manager.existing("c1") is None and manager.existing("c2") is None
    assert _fake(first).exited and _fake(second).exited


async def test_the_live_cap_evicts_the_least_recently_used():
    manager = _manager(_FakeHost(), max_live=2)
    oldest, middle, newest = (_live(manager, k) for k in ("c1", "c2", "c3"))
    oldest.last_used -= 100  # make the LRU order unambiguous
    middle.last_used -= 50

    await manager.enforce_cap()

    assert manager.existing("c1") is None
    assert manager.existing("c2") is middle
    assert manager.existing("c3") is newest
    assert _fake(oldest).exited


async def test_creation_locks_do_not_accumulate_forever():
    # Every other map here is bounded or reaped; the per-conversation creation locks were
    # the one structure that only ever grew — an entry per conversation that ever browsed.
    manager = _manager(_FakeHost(cdp_url=None), idle_ttl_s=0.0)
    for key in ("c1", "c2", "c3"):
        _ = await manager.acquire(key)  # degrades (no browser), but takes a lock on the way
    assert len(manager.creating) == 3

    live = _live(manager, "c4")
    _ = manager.creating.setdefault("c4", asyncio.Lock())
    await manager.sweep()

    # The three that hold nothing are gone; c4's went with its own reaped session.
    assert manager.creating == {}
    assert _fake(live).exited


async def test_stop_tears_every_session_down():
    manager = _manager(_FakeHost())
    live = _live(manager, "c1")
    await manager.stop()
    assert manager.existing("c1") is None
    assert _fake(live).exited


async def test_teardown_leaves_the_shared_window_alone():
    # The window belongs to the host browser and other conversations — and the operator —
    # are still in it; a session that merely attached must never close it.
    session = ControlledBrowserSession(cdp_url="http://127.0.0.1:9222")
    closed: list[bool] = []

    class _Handle:
        async def close(self) -> None:
            closed.append(True)

    session._browser = cast(Browser, cast(object, _Handle()))  # noqa: SLF001 — the handle teardown would close
    await session.__aexit__(None, None, None)

    assert closed == []


# --- the tools -------------------------------------------------------------------------


def _ctx(caps: ServiceContainer, conversation_id: str) -> RunContext[RunDeps]:
    run = Run(id=f"run-{conversation_id}", kind="chat", owner_id=OWNER, stream=RunStream())
    deps = RunDeps(run=run, owner_id=OWNER, caps=caps, conversation_id=conversation_id)
    return RunContext(deps=deps, model=TestModel(), usage=RunUsage())


async def test_the_catalog_matches_the_harness_toolset():
    # The namespaced names are written out in `tools/browse.py` for the offline and vision
    # gates; if the harness renames a tool, the gate would silently stop covering it.
    assert set(_browse().tools) == TOOL_NAMES


async def test_the_tools_degrade_when_there_is_no_session_manager():
    toolset = _browse()
    refusal = await toolset.bind("navigate", _ctx(ServiceContainer(), "c1"))
    assert isinstance(refusal, str) and "not available" in refusal


async def test_the_tools_degrade_when_no_browser_can_be_attached():
    caps = ServiceContainer()
    caps.add(_manager(_FakeHost(cdp_url=None)))
    refusal = await _browse().bind("navigate", _ctx(caps, "c1"))
    assert isinstance(refusal, str) and "not available" in refusal


class _RecordingToolset[D](PlaywrightBrowserToolset[D]):
    """The harness toolset, remembering the session it was built over (the harness keeps
    its own reference private)."""

    def __init__(self, *, session: PlaywrightBrowserSession) -> None:
        super().__init__(session=session)
        self.bound_session: PlaywrightBrowserSession = session


async def test_two_conversations_drive_two_pages_not_two_toolsets(
    monkeypatch: pytest.MonkeyPatch,
):
    # The trap `tools/rebound.py` documents: asserting only "the toolsets differ" passes
    # while both dispatch onto the template's page. Assert the *pages* differ and each is
    # its own conversation's.
    monkeypatch.setattr(browse_module, "PlaywrightBrowserToolset", _RecordingToolset)
    manager = _manager(_FakeHost())
    caps = ServiceContainer()
    caps.add(manager)
    first_live, second_live = _live(manager, "c1"), _live(manager, "c2")
    toolset = _browse()

    first = await toolset.bind("navigate", _ctx(caps, "c1"))
    second = await toolset.bind("navigate", _ctx(caps, "c2"))

    assert isinstance(first, _RecordingToolset)
    assert isinstance(second, _RecordingToolset)
    assert first is not second
    assert first.bound_session is first_live.session
    assert second.bound_session is second_live.session


async def test_a_new_session_gets_a_new_binding():
    # A reaped conversation re-attaches under a *new* session; a toolset cached by
    # conversation would keep driving the dead one.
    manager = _manager(_FakeHost())
    caps = ServiceContainer()
    caps.add(manager)
    toolset = _browse()

    _ = _live(manager, "c1")
    before = await toolset.bind("navigate", _ctx(caps, "c1"))
    await manager.release("c1")
    _ = _live(manager, "c1", page=_FakePage(url="https://example.com/after"))
    manager.sessions["c1"].token = "token-fresh"  # a genuinely new session
    after = await toolset.bind("navigate", _ctx(caps, "c1"))

    assert before is not after


@pytest.mark.parametrize("name", sorted(TOOL_NAMES))
def test_every_tool_is_declared_network_dependent(name: str):
    # Every browse tool *is* the internet: offline mode must withhold all of them, not
    # the subset someone remembered.
    from tools.browse import NETWORK_TOOLS

    assert f"browse_{name}" in NETWORK_TOOLS


# --- the operator closing the window ----------------------------------------------------


async def test_a_window_the_operator_closed_is_not_silently_reopened(
    attached: list[_FakeSession],
):
    # The whole point: closing the window is how somebody says they are done with it, and
    # the next tool call must not answer that by launching a replacement on their screen.
    host = _FakeHost()
    manager = _manager(host)
    live = _live(manager, "c1")
    _context(live).pages.clear()  # they closed the last tab

    assert await manager.acquire("c1", reopen=False) is None
    assert manager.closed("c1")
    assert attached == []  # ...and nothing was launched to replace it


async def test_an_explicit_reopen_starts_a_fresh_window(attached: list[_FakeSession]):
    # The way back. Something explicit — the model's own `navigate`, or the operator's
    # "open the browser" control — is allowed to start over, and doing so clears the record.
    manager = _manager(_FakeHost())
    live = _live(manager, "c1")
    _context(live).pages.clear()
    assert await manager.acquire("c1", reopen=False) is None

    reopened = await manager.acquire("c1", reopen=True)

    assert reopened is not None and reopened is not live
    assert not manager.closed("c1")
    assert len(attached) == 1


async def test_the_refusal_stands_until_something_actually_reopens(
    attached: list[_FakeSession],
):
    # Not consumed by the first refusal. "They are done with this" holds until somebody
    # says otherwise, or the second browse tool of the same turn would quietly reopen the
    # window the first one just reported as closed.
    manager = _manager(_FakeHost())
    live = _live(manager, "c1")
    _context(live).pages.clear()

    for _ in range(3):
        assert await manager.acquire("c1", reopen=False) is None
    assert attached == []


@pytest.mark.usefixtures("attached")
async def test_an_idle_reap_leaves_no_such_record():
    # Ours, not theirs. An idle session is reaped to save a window and a driver, and the
    # next call re-attaches with the login restored — the model never needed to hear about
    # it, and reporting it as "the operator closed the browser" would be a lie.
    manager = _manager(_FakeHost(), idle_ttl_s=0.0)
    _ = _live(manager, "c1")

    await manager.sweep()

    assert manager.existing("c1") is None
    assert not manager.closed("c1")
    assert await manager.acquire("c1", reopen=False) is not None


@pytest.mark.usefixtures("attached")
async def test_the_browser_going_away_is_not_the_operators_doing():
    # A VM reset or offline mode is ours, so the next tool call degrades or re-attaches
    # rather than telling the model the operator ended it.
    host = _FakeHost()
    manager = _manager(host)
    _ = _live(manager, "c1")
    host.fake_cdp_url = None

    await manager.sweep()

    assert not manager.closed("c1")


@pytest.mark.usefixtures("attached")
async def test_the_operator_ending_the_browser_is_recorded():
    manager = _manager(_FakeHost())
    _ = await manager.acquire("c1")

    await manager.close("c1")

    assert manager.existing("c1") is None
    assert manager.closed("c1")
    assert await manager.acquire("c1", reopen=False) is None


async def test_only_navigate_may_reopen_after_a_close(attached: list[_FakeSession]):
    # A `click` on a page that no longer exists must not open a blank window and let the
    # model believe the click landed; a `navigate` is it deliberately starting somewhere.
    caps = ServiceContainer()
    manager = _manager(_FakeHost())
    caps.add(manager)
    live = _live(manager, "c1")
    _context(live).pages.clear()
    toolset = _browse()

    refusal = await toolset.bind("click", _ctx(caps, "c1"))

    assert isinstance(refusal, str)
    assert "ended this conversation's browser" in refusal
    assert "not available" not in refusal  # ...the *other* degrade, which would be wrong
    assert attached == []

    assert not isinstance(await toolset.bind("navigate", _ctx(caps, "c1")), str)


# --- what the model is told about the browser -------------------------------------------


async def test_a_thread_with_no_browser_is_told_nothing():
    # The category is dormant because eighteen schemas on every request is the most
    # expensive thing in the catalog; a page of prose about them on every thread that never
    # browses would undo that.
    caps = ServiceContainer()
    caps.add(_manager(_FakeHost()))
    assert await browse_instructions(_ctx(caps, "c1")) == ""


async def test_a_thread_with_no_browser_feature_at_all_is_told_nothing():
    assert await browse_instructions(_ctx(ServiceContainer(), "c1")) == ""


async def test_an_open_browser_brings_the_harness_guidance_and_ours():
    # The bug this closes: taking `PlaywrightBrowserToolset` without `PlaywrightBrowser`
    # left `get_instructions` behind, so the model got eighteen tool schemas and no account
    # of how they fit together — not even that `aria-ref` handles come from `snapshot`.
    caps = ServiceContainer()
    manager = _manager(_FakeHost())
    caps.add(manager)
    _ = _live(manager, "c1")

    text = await browse_instructions(_ctx(caps, "c1"))

    assert "aria-ref" in text  # the harness's half, borrowed rather than restated
    assert "tabs('new')" in text  # ...and ours: opening a tab is a thing it may do
    assert "end the browser" in text  # ...and that the operator can end it


@pytest.mark.parametrize("phrase", ["snapshot", "iframe", "wait_for", "scroll"])
async def test_the_harness_guidance_is_borrowed_whole(phrase: str):
    # Restating it here would mean two descriptions of eighteen tools drifting apart, and
    # the one that drifted would still be the one shipped.
    caps = ServiceContainer()
    manager = _manager(_FakeHost())
    caps.add(manager)
    _ = _live(manager, "c1")
    assert phrase in await browse_instructions(_ctx(caps, "c1"))


async def test_one_session_shares_one_toolset_so_parallel_calls_serialise():
    # Tool calls within one model response run concurrently, and every browse tool acts on
    # the *active tab* — so two of them racing would interleave on one page: a snapshot
    # taken mid-navigation, a click landing after the page it was aimed at is gone. What
    # prevents that is the harness's own `_operation_lock`, which is per toolset instance,
    # so it only serialises anything while a conversation's calls all reach the *same*
    # instance. Keyed by the session token (`bind`), they do — and a cache keyed anything
    # more finely would silently hand out a lock per call.
    caps = ServiceContainer()
    manager = _manager(_FakeHost())
    caps.add(manager)
    _ = _live(manager, "c1")
    toolset = _browse()

    first, second = (
        await toolset.bind("navigate", _ctx(caps, "c1")),
        await toolset.bind("click", _ctx(caps, "c1")),
    )

    assert isinstance(first, PlaywrightBrowserToolset)
    assert isinstance(second, PlaywrightBrowserToolset)
    assert first is second


async def test_deleting_a_conversation_forgets_that_its_window_was_closed(tmp_path: Path):
    # The tombstone is keyed by conversation and has no reaper of its own, so the one
    # place a key stops meaning anything has to drop it.
    manager = _manager(_FakeHost(), state_dir=tmp_path)
    live = _live(manager, "c1")
    _context(live).pages.clear()
    assert await manager.acquire("c1", reopen=False) is None
    assert manager.closed("c1")

    await manager.purge("c1")

    assert not manager.closed("c1")
