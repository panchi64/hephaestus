"""Source-citation events."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from .base import Body


class CitationAdded(Body):
    """A source the turn met, and how far it got with it.

    ``key`` is what this source *is* — a URL for a page, ``source:ref`` for a corpus
    passage — and is what a client folds repeat sightings by. It is on the wire rather
    than derived because "these two rows are one source" is a claim about the sources: a
    client computing it would be a client deciding, and a web citation and a corpus one do
    not fold by the same field.

    ``engagement`` is the rung of ``listed`` < ``read`` < ``cited`` this sighting reached
    (see :class:`core.citations.Engagement`). One source may arrive on several frames —
    listed by a search, then read by a fetch — and a client keeps the highest. Before this
    every hit a tool returned was emitted identically, so "cited" meant no more than "a
    tool returned it", and the operator could not tell the page the answer rests on from
    the eight beside it in a result list.

    ``url`` is null for a corpus passage, which has a locator rather than an address —
    the one non-additive part of this body, and the reason a client must read ``kind``
    before it renders a link. ``published`` is the source's own date exactly as its
    provider reported it, never parsed here; ``retrieved_at`` is when this run read it.
    ``snippet`` is the text the source was seen through, already unfenced and capped.

    Deliberately **not** carried: any link from this source to a particular sentence of
    the answer. That is a reading of the finished answer, not a property of a source.
    """

    type: Literal["citation.added"] = "citation.added"
    url: str | None = None
    title: str | None = None
    key: str = ""
    kind: Literal["web", "corpus"] = "web"
    engagement: Literal["listed", "read", "cited"] = "listed"
    snippet: str | None = None
    published: str | None = None
    retrieved_at: datetime | None = None
    #: ``kind="corpus"`` only — which indexed source the passage came from, and where in it.
    source_id: str | None = None
    ref: str | None = None
