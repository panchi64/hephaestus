"""Web tools — the agent's thin adapters over the web capabilities.

Two verbs reached through ``RunDeps``: ``search`` over
:class:`~services.search.SearchService` (SearXNG) and ``fetch`` over
:class:`~services.webfetch.BrowserFetcher` (render the page in a headless browser, extract
to Markdown). No logic here — it lives in the services. The results are typed dataclasses
Pydantic AI serializes for the model, already untrusted-wrapped by the service.

An unreadable URL raises ``ToolFailed``: the model reads why and picks another source,
and since the same fetch would fail again it spends no retry budget. A blocked address
and a missing capability (web not wired, or no provider configured) return the same
diagnostic text as a plain result, the graceful-degradation shape of the memory tools.

**A repeat is refused before it costs anything.** A thread that reads widely — a research
thread most of all — loops: it re-asks a search it already ran, or re-fetches a page whose
text is already sitting in its own context, and pays a full network round trip and a second
copy of the result for nothing. The run's :class:`~services.search.DedupeSets`
(``RunDeps.web_dedupe``) is *claimed* before the call and handed back if the call then
failed, so a search that errored or a page that refused to render stays eligible while two
parallel calls for the same thing cannot both slip past the check. The refusal says plainly
that the answer is already in the transcript, because a bare "no results" would read as
"the web has nothing" and send the model looking again.

A search's key is its whole request — the query *and* the arguments that change what comes
back — so narrowing a question to the last day is a new read rather than a repeat of the
same words. Continuing a long page is the same idea for ``fetch``: ``offset > 0`` is the
documented way to page through a truncated result, so it is never deduped.
"""

from __future__ import annotations

from typing import Literal

from pydantic_ai import FunctionToolset, RunContext, ToolFailed

from core.diagnostics import render_tool_error
from core.exceptions import DegradedCapabilityError, SSRFError, WebFetchError
from services.search import DedupeSets, SearchResults, SearchService
from services.webfetch import BrowserFetcher, FetchedPage

from .deps import RunDeps


def _release(dedupe: DedupeSets, claim: str | None) -> None:
    """Hand a claimed key back after a read that didn't happen. A no-op for the paging
    fetch that never took one, so the failure paths don't each have to say so."""
    if claim is not None:
        dedupe.release(claim)


def web_toolset() -> FunctionToolset[RunDeps]:
    toolset: FunctionToolset[RunDeps] = FunctionToolset()

    @toolset.tool
    async def search(
        ctx: RunContext[RunDeps],
        query: str,
        limit: int = 5,
        time_range: Literal["day", "week", "month", "year"] | None = None,
    ) -> SearchResults | str:
        """Searches the public web and returns ranked results, each with a title, URL and
        snippet, and `published` when the engine knows the page's date.

        Use it for anything current, public or outside what you know for certain, and
        check rather than answer from memory when a fact may have changed. For what the
        platform already holds (past conversations, memory, the operator's files), use
        `platform_search` instead.

        A snippet is the engine's excerpt, not the page: read a result with `web_fetch`
        before you rely on it or cite it. No results means the search ran and found
        nothing; conclude from that rather than retrying the same query. Repeating a
        search already run in this turn returns nothing new, so ask a different question
        or read a result you have.

        Args:
            query: What to search for, phrased as you would type it into a search
                engine.
            limit: The most results to return. Default 5.
            time_range: Restrict results to pages from the last day, week, month or
                year, for a question about recent events.
        """
        svc = ctx.deps.caps.get_optional(SearchService)
        if svc is None:
            return "Web search is unavailable."
        dedupe = ctx.deps.web_dedupe
        claim = dedupe.claim_search(query, limit=limit, time_range=time_range)
        if claim is None:
            return (
                f"Already searched {query!r} in this run — its results are above. "
                "Ask a different question or read one of the results you already have."
            )
        try:
            results = await svc.search(ctx.deps.owner_id, query, limit=limit, time_range=time_range)
        except DegradedCapabilityError as exc:
            dedupe.release(claim)
            return f"Web search is unavailable: {exc}"
        except BaseException:
            # Anything else — including the turn being cancelled mid-call — means this
            # search never produced results either, so it must stay askable.
            dedupe.release(claim)
            raise
        return results

    @toolset.tool
    async def fetch(
        ctx: RunContext[RunDeps], url: str, offset: int = 0, goal: str | None = None
    ) -> FetchedPage | str:
        """Fetches one web page, renders it, and returns its main content as Markdown.

        Use it to read a `web_search` result in full, or a URL the operator gave you. To
        click, fill a form, sign in or read a page that only appears after interaction,
        load the `browse` tools instead.

        A long page comes back up to a fixed budget, ending in a truncation notice; call
        again with the same `url` and the `offset` the notice gives to read on. A page
        that renders dynamically may shift slightly between those calls. Fetching a URL
        already read in this turn returns nothing new, so look back at it in your
        context. When a URL cannot be read you are told why; pick another source rather
        than trying variants of the same address.

        Args:
            url: The full URL, including `https://`.
            offset: Where to resume a truncated page, as its truncation notice gives it.
            goal: What you are looking for on the page. On a large page the result is
                then distilled to what is relevant to it. Omit it, or page with
                `offset`, to read the raw text.
        """
        svc = ctx.deps.caps.get_optional(BrowserFetcher)
        if svc is None:
            return render_tool_error(
                "web fetch is unavailable in this deployment",
                hints=["answer from search results and what you know, and say so"],
            )
        dedupe = ctx.deps.web_dedupe
        # `offset` is how a truncated page is continued, so a paging call is a different
        # read of the same URL rather than a repeat of one — and takes no key at all.
        first_read = offset == 0
        claim = dedupe.claim_url(url) if first_read else None
        if first_read and claim is None:
            return (
                f"Already fetched {url} in this run — its content is above. Re-read it "
                "there, pass `offset` to continue past where it was truncated, or pick "
                "a different source."
            )
        try:
            page = await svc.fetch(ctx.deps.owner_id, url, offset=offset, goal=goal)
        except SSRFError as exc:
            # A refused target is a hard boundary, not a "try again" — tell the model
            # plainly so it moves on instead of probing variants of a blocked address.
            _release(dedupe, claim)
            return render_tool_error(
                f"refused to fetch {url}: {exc}",
                hints=["this address is blocked; do not try variants of it, use another source"],
            )
        except WebFetchError as exc:
            # Terminal for this URL: the same fetch would fail again, so the model is told
            # to move on rather than asked to retry.
            _release(dedupe, claim)
            raise ToolFailed(
                render_tool_error(
                    f"could not read {url}: {exc}", hints=[exc.hint or "pick a different source"]
                )
            ) from exc
        except BaseException:
            _release(dedupe, claim)
            raise
        return page

    return toolset
