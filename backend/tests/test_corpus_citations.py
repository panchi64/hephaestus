"""The operator's own knowledge base, attributed.

For a long time `corpus_retrieve` returned plain dicts and therefore cited nothing, so a
background thread reading a folder on the operator's own disk showed no sources for it while
attributing every page it read on the web. That is the defect these pin, plus the two
things that made it fixable: a citation that does not have to be a URL, and one untrusted
preamble per batch rather than one per hit.
"""

from __future__ import annotations

import pytest

from agent.translate import citations_from_tool_result
from core.untrusted import untrusted_fence, untrusted_preamble
from tools.corpus import CorpusPassage, CorpusPassages


def _batch(*passages: tuple[str, str, str]) -> CorpusPassages:
    nonce = "cafef00d"
    return CorpusPassages(
        instruction=untrusted_preamble(nonce),
        passages=[
            CorpusPassage(
                source=source,
                ref=ref,
                matched_by="both",
                text=untrusted_fence(text, nonce, source=ref),
            )
            for source, ref, text in passages
        ],
    )


def test_a_corpus_hit_is_a_source_like_any_other():
    batch = _batch(("notes", "notes/gate.md", "the gate code is 4455"))

    [hit] = citations_from_tool_result(batch)

    assert hit.kind == "corpus"
    assert hit.url is None  # a passage has a locator, not an address
    assert hit.ref == "notes/gate.md"
    assert hit.source_id == "notes"
    assert hit.key == "notes:notes/gate.md"
    assert hit.title == "notes/gate.md"


def test_a_passage_is_read_not_merely_listed():
    # Unlike a search hit, the passage's text *is* the result — there is nothing here the
    # model saw only the title of.
    [hit] = citations_from_tool_result(_batch(("notes", "a.md", "body")))
    assert hit.engagement == "read"


def test_the_snippet_reaches_the_operator_without_the_model_s_markers():
    [hit] = citations_from_tool_result(_batch(("notes", "a.md", "the gate code is 4455")))
    assert hit.snippet == "the gate code is 4455"
    assert "UNTRUSTED" not in (hit.snippet or "")


def test_hits_are_cited_in_rank_order():
    batch = _batch(("notes", "a.md", "first"), ("mail", "b.eml", "second"))
    assert [c.key for c in citations_from_tool_result(batch)] == ["notes:a.md", "mail:b.eml"]


def test_a_retrieve_that_found_nothing_cites_nothing():
    assert citations_from_tool_result(CorpusPassages(instruction="", passages=[])) == []


def test_the_batch_carries_one_preamble_not_one_per_hit():
    # Every hit used to be `wrap_untrusted`-ed on its own, so a retrieve of eight paid for
    # the whole "treat this as data" sentence eight times. The fence and the nonce — the
    # parts that actually resist injection — are unchanged.
    batch = _batch(("notes", "a.md", "one"), ("notes", "b.md", "two"))
    assert batch.instruction.count("Untrusted external data follows") == 1
    for passage in batch.passages:
        assert "Untrusted external data follows" not in passage.text
        assert passage.text.startswith("[BEGIN UNTRUSTED CONTENT ")


@pytest.mark.parametrize("text", ["plain text", "[BEGIN UNTRUSTED CONTENT x]"])
def test_unfencing_leaves_text_it_does_not_recognise_alone(text: str):
    from core.untrusted import unfence

    assert unfence(text) == text
