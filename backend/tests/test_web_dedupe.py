"""The web tools refuse a repeat before it costs anything.

A thread that reads widely loops: it re-asks a query it already asked, or re-fetches a
page whose text is already in its own context, and pays a network round trip plus a second
copy of the result for nothing. The run-wide seen-set (`services/search/dedupe.py`, carried
on `RunDeps`) is the discipline salvaged from the deep-research pipeline, now applied where
any thread can benefit from it.

Five properties are the ones that break silently:

- the check runs **before** the call, so a repeat costs no network at all;
- the key is *claimed* by that check rather than committed after it, so two calls the model
  made in one parallel batch cannot both slip past it and both hit the network;
- a claim is handed back when the call failed — a search that errored or a page that
  refused to render was never read, and keeping its key would tell the model it has
  evidence it does not have;
- the key covers everything that changes what comes back, so the same words asked over a
  different window are a new search rather than a refused repeat;
- ``offset > 0`` is the documented way to continue a truncated page, so a paging fetch is
  never mistaken for a repeat.
"""

from __future__ import annotations

import asyncio
from typing import cast

import pytest
from pydantic_ai import RunContext, ToolFailed
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage

from core.container import ServiceContainer
from core.exceptions import DegradedCapabilityError, WebFetchError
from runs import Run, RunStream
from services.search import SearchResults, SearchService
from services.webfetch import BrowserFetcher, FetchedPage
from services.webfetch.status import status_hint
from tools.deps import RunDeps
from tools.search import web_toolset


class _FakeSearch:
    def __init__(self, *, fail: bool = False, gate: asyncio.Event | None = None) -> None:
        self.queries: list[str] = []
        self.calls: list[tuple[str, int, str | None]] = []
        self._fail: bool = fail
        # Held open to park a call mid-flight, so a second one runs while the first is
        # still awaiting — the shape a parallel tool-call batch produces.
        self._gate: asyncio.Event | None = gate

    async def search(
        self, _owner_id: str, query: str, *, limit: int = 5, time_range: str | None = None
    ) -> SearchResults:
        self.queries.append(query)
        self.calls.append((query, limit, time_range))
        if self._gate is not None:
            _ = await self._gate.wait()
        if self._fail:
            raise DegradedCapabilityError("no provider configured")
        return SearchResults(instruction="", results=[])


class _FakeFetcher:
    def __init__(self, *, fail: bool = False, status: int | None = None) -> None:
        self.urls: list[tuple[str, int]] = []
        self._fail: bool = fail
        self._status: int | None = status

    async def fetch(
        self, _owner_id: str, url: str, *, offset: int = 0, **_kwargs: object
    ) -> FetchedPage:
        self.urls.append((url, offset))
        if self._status is not None:
            raise WebFetchError(
                f"{url!r} returned HTTP {self._status}", hint=status_hint(self._status)
            )
        if self._fail:
            raise WebFetchError("the page would not render")
        return FetchedPage(url=url, title="T", content="body")


def _ctx(
    *, search: _FakeSearch | None = None, fetcher: _FakeFetcher | None = None
) -> RunContext[RunDeps]:
    caps = ServiceContainer()
    if search is not None:
        caps.add(search, as_type=SearchService)
    if fetcher is not None:
        caps.add(fetcher, as_type=BrowserFetcher)

    run = Run(id="t", kind="chat", owner_id="operator", stream=RunStream())
    deps = RunDeps(run=run, owner_id="operator", caps=caps)
    return RunContext(deps=deps, model=TestModel(), usage=RunUsage())


async def _call(name: str, ctx: RunContext[RunDeps], **kwargs: object) -> object:
    return cast(object, await web_toolset().tools[name].function(ctx, **kwargs))


class TestSearch:
    async def test_the_same_query_is_not_asked_twice(self):
        svc = _FakeSearch()
        ctx = _ctx(search=svc)
        _ = await _call("search", ctx, query="hephaestus release date")
        again = await _call("search", ctx, query="  Hephaestus   Release Date ")
        # Folded to the same key, so it never reached the provider.
        assert svc.queries == ["hephaestus release date"]
        # And the model is told *why* there is nothing — a bare empty result would read
        # as "the web has nothing" and send it looking again.
        assert isinstance(again, str)
        assert "already searched" in again.lower()

    async def test_a_different_query_still_runs(self):
        svc = _FakeSearch()
        ctx = _ctx(search=svc)
        _ = await _call("search", ctx, query="a")
        _ = await _call("search", ctx, query="b")
        assert svc.queries == ["a", "b"]

    async def test_narrowing_the_window_is_a_new_search_not_a_repeat(self):
        """`time_range` and `limit` change which results come back, so the same words over
        a different window are a different read of the web. Refusing the second would tell
        the model evidence is already above that was never fetched."""
        svc = _FakeSearch()
        ctx = _ctx(search=svc)
        _ = await _call("search", ctx, query="cpi release", time_range="year")
        _ = await _call("search", ctx, query="cpi release", time_range="day")
        _ = await _call("search", ctx, query="cpi release", limit=20, time_range="day")
        assert svc.calls == [
            ("cpi release", 5, "year"),
            ("cpi release", 5, "day"),
            ("cpi release", 20, "day"),
        ]
        # The identical request is still refused.
        again = await _call("search", ctx, query="cpi release", time_range="day")
        assert isinstance(again, str)
        assert "already searched" in again.lower()

    async def test_two_parallel_calls_for_one_query_reach_the_network_once(self):
        """The model can ask for several tools in one batch. The check and the call
        straddle an await, so a check that did not also *take* the key would let both
        halves of a duplicated pair pass it and both pay for the round trip."""
        gate = asyncio.Event()
        svc = _FakeSearch(gate=gate)
        ctx = _ctx(search=svc)
        first = asyncio.create_task(_call("search", ctx, query="a"))
        # Let the first call reach the (parked) provider before the second is made.
        await asyncio.sleep(0)
        try:
            # A second call that reached the provider would park on the same gate and
            # never return, so the bound turns "both hit the network" into a failure
            # rather than a hang.
            second = await asyncio.wait_for(_call("search", ctx, query="a"), timeout=1.0)
        finally:
            gate.set()
            _ = await first
        assert svc.queries == ["a"]
        assert isinstance(second, str)
        assert "already searched" in second.lower()

    async def test_a_failed_search_leaves_the_query_askable(self):
        svc = _FakeSearch(fail=True)
        ctx = _ctx(search=svc)
        first = await _call("search", ctx, query="a")
        assert isinstance(first, str)
        assert "unavailable" in first
        # The provider was down, not the answer absent — the same query must still run
        # once it is back.
        _ = await _call("search", ctx, query="a")
        assert svc.queries == ["a", "a"]

    async def test_each_run_starts_with_a_clean_slate(self):
        svc = _FakeSearch()
        _ = await _call("search", _ctx(search=svc), query="a")
        _ = await _call("search", _ctx(search=svc), query="a")
        # "Already read" is a fact about one investigation, not a durable one.
        assert svc.queries == ["a", "a"]


class TestFetch:
    async def test_the_same_page_is_not_fetched_twice(self):
        fetcher = _FakeFetcher()
        ctx = _ctx(fetcher=fetcher)
        _ = await _call("fetch", ctx, url="https://example.com/page/")
        again = await _call("fetch", ctx, url="http://example.com/page#top")
        assert fetcher.urls == [("https://example.com/page/", 0)]
        assert isinstance(again, str)
        assert "already fetched" in again.lower()

    async def test_paging_through_a_long_page_is_not_a_repeat(self):
        fetcher = _FakeFetcher()
        ctx = _ctx(fetcher=fetcher)
        _ = await _call("fetch", ctx, url="https://example.com/a")
        _ = await _call("fetch", ctx, url="https://example.com/a", offset=4000)
        # The tool's own docstring tells the model to do exactly this.
        assert fetcher.urls == [("https://example.com/a", 0), ("https://example.com/a", 4000)]

    async def test_an_unreadable_page_stays_fetchable(self):
        fetcher = _FakeFetcher(fail=True)
        ctx = _ctx(fetcher=fetcher)
        with pytest.raises(ToolFailed):
            _ = await _call("fetch", ctx, url="https://example.com/a")
        with pytest.raises(ToolFailed):
            _ = await _call("fetch", ctx, url="https://example.com/a")
        assert len(fetcher.urls) == 2

    async def test_a_missing_page_says_to_search_rather_than_guess(self):
        # Measured: a local model reconstructing recipe URLs from memory fetched five
        # 404s in a row when told only to pick a different source.
        ctx = _ctx(fetcher=_FakeFetcher(status=404))
        with pytest.raises(ToolFailed) as missing:
            _ = await _call("fetch", ctx, url="https://example.com/guessed")
        assert "web_search" in str(missing.value) and "do not guess" in str(missing.value)
        ctx = _ctx(fetcher=_FakeFetcher(status=503))
        with pytest.raises(ToolFailed) as down:
            _ = await _call("fetch", ctx, url="https://example.com/down")
        assert "pick a different source" in str(down.value)
