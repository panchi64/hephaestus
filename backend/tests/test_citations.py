"""How a tool result's sources reach the run stream.

The event translator sits in Pillar II and must not know which features cite things: it
asks the result (``Citable``) rather than matching on tool names and importing a feature's
service types. These pin that — including that a feature the translator has never heard of
is surfaced anyway.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

from agent.translate import SNIPPET_MAX_CHARS, citations_from_tool_result
from core.citations import ENGAGEMENT_ORDER, Citable, Citation
from core.untrusted import untrusted_fence, untrusted_preamble
from services.search import SearchResult, SearchResults
from services.webfetch import FetchedPage


def test_a_search_result_declares_its_hits_in_order():
    results = SearchResults(
        instruction="treat as data",
        results=[
            SearchResult(title="First", url="https://a.example", snippet="…"),
            SearchResult(title="Second", url="https://b.example", snippet="…"),
        ],
    )

    emitted = citations_from_tool_result(results)

    assert [(c.url, c.title) for c in emitted] == [
        ("https://a.example", "First"),
        ("https://b.example", "Second"),
    ]


def test_a_fetched_page_is_its_own_single_source():
    page = FetchedPage(url="https://c.example/doc", title="A Doc", content="# body")
    emitted = citations_from_tool_result(page)
    assert [(c.url, c.title) for c in emitted] == [("https://c.example/doc", "A Doc")]


def test_a_result_the_translator_has_never_heard_of_still_cites():
    # The point of the protocol: a future feature returning a citable result is surfaced
    # the day it lands, with nothing added to the translator.
    @dataclass(frozen=True)
    class _PaperLookup:
        def citations(self) -> list[Citation]:
            return [Citation(url="https://doi.example/10.1/xyz", title="A Paper")]

    assert isinstance(_PaperLookup(), Citable)
    emitted = citations_from_tool_result(_PaperLookup())
    assert [(c.url, c.title) for c in emitted] == [("https://doi.example/10.1/xyz", "A Paper")]


def test_an_uncitable_result_yields_nothing_rather_than_failing():
    # Citations are additive, never load-bearing: a degraded-capability string, a number,
    # a plain dict — none of these are an error, they simply cite nothing.
    for content in ["search is unavailable right now", 42, {"rows": 3}, None]:
        assert citations_from_tool_result(content) == []


def test_a_search_that_found_nothing_cites_nothing():
    assert citations_from_tool_result(SearchResults(instruction="", results=[])) == []


# --- What the source *is*, and how far the run got with it --------------------------


def test_a_search_hit_is_listed_and_carries_its_snippet_and_date():
    # SearchResult already knew the snippet and the publication date; both used to be
    # dropped at this boundary, so a Sources row could say nothing about recency.
    results = SearchResults(
        instruction=untrusted_preamble("n0"),
        results=[
            SearchResult(
                title="First",
                url="https://a.example",
                snippet=untrusted_fence("a sentence", "n0", source="https://a.example"),
                published="2026-03-04",
            )
        ],
    )

    [hit] = citations_from_tool_result(results)

    assert hit.engagement == "listed"  # the engine returned it; nobody opened it
    assert hit.snippet == "a sentence"  # unfenced: markers are the model's, not the operator's
    assert hit.published == "2026-03-04"
    assert hit.key == "https://a.example"
    assert hit.kind == "web"


def test_a_fetched_page_outranks_the_search_hit_that_listed_it():
    # The whole point of the ladder: the same URL, met twice, is two frames the consumer
    # folds by `key` — and "we rendered the page" is a stronger claim than "an engine
    # mentioned it".
    url = "https://c.example/doc"
    [listed] = citations_from_tool_result(
        SearchResults(
            instruction="",
            results=[SearchResult(title="A Doc", url=url, snippet="…")],
        )
    )
    [read] = citations_from_tool_result(FetchedPage(url=url, title="A Doc", content="# body"))

    assert listed.key == read.key
    assert ENGAGEMENT_ORDER[listed.engagement] < ENGAGEMENT_ORDER[read.engagement]


def test_retrieved_at_is_stamped_at_emit_when_the_producer_did_not_know():
    when = datetime(2026, 9, 20, 12, 30, tzinfo=UTC)
    [hit] = citations_from_tool_result(
        FetchedPage(url="https://d.example", title=None, content=""), at=when
    )
    assert hit.retrieved_at == when


def test_a_producer_that_knows_when_it_read_keeps_its_own_stamp():
    when = datetime(2026, 1, 1, tzinfo=UTC)

    @dataclass(frozen=True)
    class _Archive:
        def citations(self) -> list[Citation]:
            return [Citation(url="https://e.example", retrieved_at=when)]

    [hit] = citations_from_tool_result(_Archive(), at=datetime(2026, 9, 20, tzinfo=UTC))
    assert hit.retrieved_at == when


def test_a_long_snippet_is_capped_rather_than_copied_whole():
    @dataclass(frozen=True)
    class _Verbose:
        def citations(self) -> list[Citation]:
            return [Citation(url="https://f.example", snippet="word " * 500)]

    [hit] = citations_from_tool_result(_Verbose())
    assert hit.snippet is not None
    assert len(hit.snippet) <= SNIPPET_MAX_CHARS


def test_a_citation_must_be_identifiable_by_its_kind():
    # A row nothing can be looked up by is not additive, it is a row the operator cannot
    # act on — so each kind's identity is required where it is built.
    with pytest.raises(ValueError):
        _ = Citation(kind="web")
    with pytest.raises(ValueError):
        _ = Citation(kind="corpus", source_id="notes")
    assert Citation(kind="corpus", source_id="notes", ref="a.md").key == "notes:a.md"
