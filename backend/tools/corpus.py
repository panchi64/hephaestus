"""Corpus tool — a search inside files the operator named.

A single ``retrieve`` verb over specific file sources by id (a file attached to the
conversation, whose id is in its attachment marker), a thin pass-through to
:class:`~services.corpus.CorpusIndex`. Searching the whole corpus is ``platform_search``.

Each hit's text is folder/file content — **external data**, so it is fenced with
:func:`core.untrusted.untrusted_fence` before it reaches the model (the corpus is an
untrusted-content ingester, like web). A missing capability degrades to an "unavailable"
message rather than failing.

**The result is citable, and for a long time it was not.** A research thread reading the
operator's own knowledge base — the one corpus they have most reason to trust — showed no
sources for it at all, because this tool handed back plain dicts and
:func:`agent.translate.citations_from_tool_result` asks the *result* what it cites. Not a
missing feature: a page the agent read on the web was attributed and a file on their own
disk was not, which reads as the file never having been opened. So the hits come back as
:class:`CorpusPassages`, which declares them at the ``read`` rung — a passage is not
listed-and-skipped, it is text that went in front of the model.

**One preamble, not one per hit.** The batch carries a single
:func:`~core.untrusted.untrusted_preamble` over a shared nonce, the shape
:class:`~services.search.SearchResults` already uses. Fencing each hit with
``wrap_untrusted`` repeated the whole "treat this as data" instruction once per passage,
so a retrieve of eight paid for it eight times. The fence, the nonce and the marker
discipline — the parts that actually resist injection — are untouched.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic_ai import FunctionToolset, ModelRetry, RunContext

from core.citations import Citation
from core.diagnostics import render_tool_error
from core.untrusted import new_nonce, unfence, untrusted_fence, untrusted_preamble
from services.corpus import CorpusIndex
from services.projects import visible_project_ids

from .deps import RunDeps


@dataclass(frozen=True)
class CorpusPassage:
    """One retrieved passage as the model reads it.

    ``source`` and ``ref`` are the same pair the index identifies a hit by, and together
    they are the passage's citation identity — the corpus has locators where the web has
    addresses, which is why a citation cannot be a URL and nothing else.
    """

    source: str
    ref: str
    matched_by: str
    #: The passage, inside this batch's untrusted fence.
    text: str


@dataclass(frozen=True)
class CorpusPassages:
    """A whole retrieve call's hits. ``instruction`` is the single untrusted-content
    preamble for the batch (``""`` when there were none)."""

    instruction: str
    passages: list[CorpusPassage]

    def citations(self) -> list[Citation]:
        """The passages, in rank order — what the operator's own knowledge contributed.

        ``read`` rather than ``listed``: unlike a search hit, a passage's text is the
        result. There is nothing here the model saw only the title of.

        The snippet is unfenced, for the reason given in
        :func:`core.untrusted.unfence` — the markers are how the *model* is handed
        external text, and a citation row is read by the operator. The translator caps
        its length; a passage is not re-copied whole onto the stream.

        A passage with no ``ref`` is dropped rather than cited, the symmetric case to a
        search hit with no URL: the locator *is* a corpus citation's identity, and
        :class:`~core.citations.Citation` refuses one without it. The index has always
        given every hit a ref, so this guards a shape rather than an observed failure —
        but it guards it here, where losing one row is the cost, instead of inside the
        translator's citation pass, where the raise would cost the whole batch.
        """
        return [
            Citation(
                kind="corpus",
                title=item.ref,
                source_id=item.source,
                ref=item.ref,
                snippet=unfence(item.text),
                engagement="read",
            )
            for item in self.passages
            if item.ref
        ]


def corpus_toolset() -> FunctionToolset[RunDeps]:
    toolset: FunctionToolset[RunDeps] = FunctionToolset()

    @toolset.tool
    async def retrieve(
        ctx: RunContext[RunDeps],
        query: str,
        source_ids: list[str],
        limit: int = 8,
    ) -> CorpusPassages | str:
        """Searches inside specific files you have the ids of, such as a file attached to
        this conversation, and returns the passages most relevant to `query`, matched by
        meaning with a keyword fallback.

        Use it to find what a long document says about something without reading all
        of it. To search everything the platform holds, use `platform_search`; to work on
        the file itself with code, stage it with `attachments_provision`. A file excluded
        from the knowledge base can still be searched here by id.

        Returns each passage with its `source`, a `ref` locating it in the file, and how
        it was matched.

        Args:
            query: What you are looking for, in plain words.
            source_ids: The files to search, by the ids their attachment notes give.
            limit: The most passages to return. Default 8.
        """
        if not source_ids:
            raise ModelRetry(
                render_tool_error(
                    "no `source_ids` were given",
                    hints=[
                        "pass the ids of the files to search",
                        "to search everything, use `platform_search`",
                    ],
                )
            )
        index = ctx.deps.caps.get_optional(CorpusIndex)
        if index is None:
            # A plain string, like the web tools' degrade: an empty batch would read to
            # the model as "your knowledge base has nothing on this", which is a different
            # and much more misleading answer than "it is not available".
            return "The knowledge corpus is unavailable."
        # The run's own project, not the operator's live selection: a turn keeps reading
        # from the project it started in even if they switch away mid-answer. Unfiled
        # sources stay reachable either way — only another project's are excluded.
        hits = await index.retrieve(
            ctx.deps.owner_id,
            query,
            source_ids=source_ids,
            limit=limit,
            visible_projects=visible_project_ids(ctx.deps.project_id),
        )
        nonce = new_nonce()
        passages = [
            CorpusPassage(
                source=hit.source_id,
                ref=hit.ref,
                matched_by=hit.matched_by,
                text=untrusted_fence(hit.text, nonce, source=hit.ref),
            )
            for hit in hits
        ]
        return CorpusPassages(
            instruction=untrusted_preamble(nonce) if passages else "",
            passages=passages,
        )

    return toolset
