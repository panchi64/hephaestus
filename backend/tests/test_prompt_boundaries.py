"""The prohibition register and the model-facing text stay in step.

A prohibition added without an entry, or an entry whose sentence a rewrite removed, fails
here; ``docs/voice.md`` §16 says which kind of prohibition earns an entry.
"""

from __future__ import annotations

from collections import Counter
from functools import cache

import pytest

from devkit.prompts import Source, corpus
from evals.harness.backlog import read_backlog
from prompts.boundaries import Boundary, normalized, prohibitions, register


@cache
def _sources() -> dict[str, str]:
    return {source.name: normalized(source.text) for source in _corpus()}


@cache
def _corpus() -> tuple[Source, ...]:
    return tuple(corpus())


def _home(sentence: str) -> str:
    """The shortest source holding ``sentence``, which is where an entry should point."""
    holding = [name for name, text in _sources().items() if sentence in text]
    return min(holding, key=lambda name: len(_sources()[name]))


def _entries() -> tuple[Boundary, ...]:
    return register().boundary


def test_every_prohibition_is_registered():
    registered = {entry.text for entry in _entries()}
    found = {sentence for source in _corpus() for sentence in prohibitions(source.text)}
    missing = sorted(found - registered)
    assert not missing, (
        "Unregistered prohibitions. Add each to prompts/register/ with its kind and why, "
        + "or rewrite it as a fact or a principle:\n"
        + "\n".join(f"  [{_home(sentence)}] {sentence}" for sentence in missing)
    )


def test_every_entry_is_still_in_its_source():
    stale = [
        f"  {entry.id} [{entry.source}] {entry.text}"
        for entry in _entries()
        if entry.text not in _sources().get(entry.source, "")
    ]
    assert not stale, (
        "Entries whose sentence is gone from their source. Update `text` and `source` to "
        + "where it lives now, or delete the entry along with the sentence:\n" + "\n".join(stale)
    )


def test_ids_and_texts_are_unique():
    for field, values in (
        ("id", [entry.id for entry in _entries()]),
        ("text", [entry.text for entry in _entries()]),
    ):
        repeated = [value for value, n in Counter(values).items() if n > 1]
        assert not repeated, f"duplicate {field}: {repeated}"


@pytest.mark.parametrize("entry", _entries(), ids=[entry.id for entry in _entries()])
def test_entry_says_why_it_is_kept(entry: Boundary):
    if entry.kind != "fact":
        assert entry.why, f"{entry.id}: a {entry.kind} entry says why the principle can't carry it"
    if entry.kind == "earned":
        assert entry.evidence, f"{entry.id}: an earned entry names the issues that earned it"
        assert entry.principle, f"{entry.id}: an earned entry names the principle it backs"


def test_evidence_names_backlog_issues():
    known = {issue.id for issue in read_backlog().entries}
    unknown = {
        issue for entry in _entries() for issue in entry.evidence if issue not in known
    }
    assert not unknown, f"evidence names issues the backlog doesn't hold: {sorted(unknown)}"
