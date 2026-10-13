"""Run-wide search/URL dedupe — normalized-exact, scoped to one run."""

from __future__ import annotations

from services.search import DedupeSets, canonicalize_url, normalize_query, search_key


def test_normalize_query_folds_case_and_whitespace():
    assert normalize_query("  Foo   Bar  ") == "foo bar"
    assert normalize_query("foo bar") == "foo bar"


def test_search_key_separates_requests_that_return_different_results():
    """The key is the whole request. Two calls that differ only in `time_range` or
    `limit` read different slices of the web, and collapsing them would refuse the second
    while telling the model its answer is already above."""
    base = search_key("cpi release", limit=5, time_range=None)
    assert base == search_key("  CPI   Release ", limit=5, time_range=None)
    assert base != search_key("cpi release", limit=5, time_range="year")
    assert search_key("cpi release", limit=5, time_range="year") != search_key(
        "cpi release", limit=5, time_range="day"
    )
    assert base != search_key("cpi release", limit=20, time_range=None)
    # A blank query has no key at all, so a caller can treat it like a repeat.
    assert search_key("   ", limit=5, time_range=None) == ""


def test_canonicalize_url_folds_scheme_trailing_slash_and_fragment():
    assert canonicalize_url("https://Example.com/Path/") == canonicalize_url(
        "http://example.com/Path#section"
    )
    # A different path is still a different resource.
    assert canonicalize_url("https://example.com/a") != canonicalize_url("https://example.com/b")
    # The query string is kept — it can change what a page serves.
    assert canonicalize_url("https://example.com/a?x=1") != canonicalize_url(
        "https://example.com/a?x=2"
    )


def test_a_key_is_claimed_once():
    dedupe = DedupeSets()
    assert dedupe.claim_search("Hephaestus release date", limit=5, time_range=None) is not None
    # Same, folded — already claimed.
    assert dedupe.claim_search("  hephaestus   release date ", limit=5, time_range=None) is None
    # Same words, different slice of the web — a read of its own.
    assert dedupe.claim_search("hephaestus release date", limit=5, time_range="day") is not None
    assert dedupe.claim_search("hephaestus pricing", limit=5, time_range=None) is not None

    assert dedupe.claim_url("https://example.com/page/") is not None
    assert dedupe.claim_url("https://example.com/page") is None  # same, sans slash
    assert dedupe.claim_url("https://example.com/other") is not None


def test_blank_input_is_never_claimable():
    dedupe = DedupeSets()
    assert dedupe.claim_search("   ", limit=5, time_range=None) is None
    assert dedupe.claim_url("") is None


def test_a_released_key_is_claimable_again():
    """The claim is taken *before* the network call, so the caller whose call then failed
    has to hand it back — a search that errored was never read, and keeping its key would
    tell the model it has evidence it does not have."""
    dedupe = DedupeSets()
    claim = dedupe.claim_search("hephaestus pricing", limit=5, time_range=None)
    assert claim is not None
    assert dedupe.claim_search("hephaestus pricing", limit=5, time_range=None) is None
    dedupe.release(claim)
    assert dedupe.claim_search("hephaestus pricing", limit=5, time_range=None) is not None

    url_claim = dedupe.claim_url("https://example.com/a")
    assert url_claim is not None
    dedupe.release(url_claim)
    assert dedupe.claim_url("https://example.com/a") is not None
