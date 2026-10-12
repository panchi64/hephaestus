"""Sources a tool result carries.

Some tool results are *about* places a claim could be checked — a search's hits, a fetched
page, a passage out of the operator's own knowledge base — and the run stream surfaces
those as citations so the answer can show its sources. The question is who knows which
results those are.

Not the event translator. It sits in Pillar II, turning the library's run into our
protocol, and a translator that matches on ``"web_search"`` and imports
``services.search`` has to be edited every time a feature grows a tool that cites
something — and knows about a feature's own types to do it. So the result type declares
its own sources instead: anything with a ``citations()`` method is citable, and the
translator asks rather than recognizes.

**A source is not only a URL.** The corpus is a first-class place to have read something,
and a citation that could only be a web address left a research thread reading the
operator's own files showing no sources for them at all. So a citation carries a ``kind``
and an identity (:attr:`Citation.key`) that a corpus passage can satisfy as well as a page
can.

**And "cited" is not one thing.** A search returns ten hits; the model read the snippets
of all ten, opened two, and wrote its answer from one. Emitting the same event for all
three cases makes "cited" mean no more than "a tool returned it", which is what
:class:`Engagement` exists to fix: ``listed`` < ``read`` < ``cited``, and a source the run
meets twice is emitted twice — once per rung it reaches. Consumers fold by
:attr:`Citation.key` and keep the highest rung, which is why the rung is on the wire and
the ladder's order is stated here rather than inferred.

What is deliberately *not* here is a link from a source to the sentence it supports: a
tool knows what it returned, and only a reader of the *finished answer* knows what the
answer ended up resting on.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol, runtime_checkable

#: Where a source lives. ``web`` is a URL; ``corpus`` is a passage out of the operator's
#: own indexed knowledge (a folder file, an upload, memory, a past conversation) and has a
#: locator rather than an address.
CitationKind = Literal["web", "corpus"]

#: How far the run actually got with a source.
#:
#: ``listed`` — it came back in a result list and the model saw its title and snippet.
#: A search hit is this, and nothing more: the page itself was never opened.
#:
#: ``read`` — its content was retrieved in full and put in front of the model. A fetched
#: page and a retrieved corpus passage are this.
#:
#: ``cited`` — something asserted rests on it, and says so. Only a producer that genuinely
#: knows this may claim it; the tool boundary does not, which is exactly the defect this
#: ladder exists to stop repeating. Nothing in this installation mints it today.
Engagement = Literal["listed", "read", "cited"]

#: The ladder's order, for a consumer folding several sightings of one source. Exported
#: rather than left to be re-derived, because "which of these two is the stronger claim"
#: is a fact about the vocabulary and not a rendering choice.
ENGAGEMENT_ORDER: dict[Engagement, int] = {"listed": 0, "read": 1, "cited": 2}


@dataclass(frozen=True)
class Citation:
    """One source a tool result points at. Deliberately not the wire event: this is what
    a capability declares, and the run stream decides how to frame it.

    Every field past ``url``/``title`` is optional because a producer declares what it
    honestly has. ``published`` is the source's own date as the provider reported it — a
    bare string, never parsed here, because a search engine's date formats are its own
    business and a half-parsed date is worse than the text. ``retrieved_at`` is when *we*
    read it; a producer that knows leaves it set, and the translator stamps the rest at
    emit time, which is within milliseconds of the truth.
    """

    url: str | None = None
    title: str | None = None
    #: The text the source was seen through — a search snippet, the opening of a passage.
    #: Already unfenced: the untrusted wrapper is how the *model* is handed external text,
    #: and a fence in a citation would be marker noise in the operator's own reading.
    snippet: str | None = None
    published: str | None = None
    retrieved_at: datetime | None = None
    kind: CitationKind = "web"
    engagement: Engagement = "listed"
    #: ``corpus`` only — which indexed source this passage came out of, and where in it.
    source_id: str | None = None
    ref: str | None = None

    def __post_init__(self) -> None:
        # A citation nothing can be looked up by is not additive, it is a row the operator
        # cannot act on — so the identity each kind needs is required at construction
        # rather than checked at the far end of the wire.
        if self.kind == "web" and not self.url:
            raise ValueError("a web citation needs a url")
        if self.kind == "corpus" and not self.ref:
            raise ValueError("a corpus citation needs a ref")

    @property
    def key(self) -> str:
        """What this source *is*, for folding repeat sightings of it into one row.

        On the wire rather than left to the consumer: "two of these are the same source"
        is a claim about the sources, and a frontend deriving it would be a frontend
        deciding. A web source is its URL; a corpus passage is its source and locator,
        which is the same ``gid`` the index already identifies a hit by.
        """
        if self.kind == "web":
            return self.url or ""
        return f"{self.source_id or ''}:{self.ref or ''}"


@runtime_checkable
class Citable(Protocol):
    """A tool result that names the sources behind it.

    Order matters — it is the order the sources are surfaced in. Folding and numbering are
    the consumer's concern (repeat sightings fold by :attr:`Citation.key`, keeping the
    highest :class:`Engagement`; the Sources row numbers by position), so an
    implementation returns what it found and nothing more.
    """

    def citations(self) -> Sequence[Citation]: ...
