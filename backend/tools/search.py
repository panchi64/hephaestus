"""Web tools — the agent's thin adapters over the web capabilities.

Two verbs reached through ``RunDeps``: ``search`` over
:class:`~services.search.SearchService` (SearXNG) and ``fetch`` over
:class:`~services.webfetch.BrowserFetcher` (render the page in a headless browser, extract
to Markdown). No logic here — it lives in the services. The results are typed dataclasses
Pydantic AI serializes for the model, already untrusted-wrapped by the service.

Failure handling leans on the engine: a *recoverable* fetch failure (a blocked or
unreadable URL) raises :class:`ModelRetry`, so Pydantic AI feeds the reason back
and the model tries a different source — bounded by the tool's retry budget. A
*missing* capability (web not wired, or no provider configured) returns a plain
message instead, the same graceful-degradation shape as the memory tools — a retry
can't fix it.

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

from pydantic_ai import FunctionToolset, ModelRetry, RunContext

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
        """Search the web for a query and return ranked results (title, URL, snippet).

        Use `time_range` to restrict results to recent pages when the question is
        time-sensitive; `published` on a result is its publication date when the engine
        knew it. No results means the search ran but found nothing — conclude from
        that rather than retrying the same query."""
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

    @toolset.tool(retries=2)
    async def fetch(
        ctx: RunContext[RunDeps], url: str, offset: int = 0, goal: str | None = None
    ) -> FetchedPage | str:
        """Fetch a single web page and return its main content as Markdown.

        Use after `web_search` to read a result in full. State the information you are
        looking for in `goal` — on large pages the result is then distilled to what's
        relevant to it; omit `goal` (or pass `offset`) to read the raw text. Fetching
        returns up to a fixed token budget of the page; when the result ends with a
        truncation notice, call `web_fetch` again with the same `url` and the `offset` the
        notice gives to continue reading (dynamic pages may shift slightly between calls).
        If a URL can't be fetched you will be told why — pick a different source."""
        svc = ctx.deps.caps.get_optional(BrowserFetcher)
        if svc is None:
            return "Web fetch is unavailable."
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
            return f"Refused: {exc}"
        except WebFetchError as exc:
            # Recoverable: the page couldn't be read — let the model pick another source.
            _release(dedupe, claim)
            raise ModelRetry(str(exc)) from exc
        except BaseException:
            _release(dedupe, claim)
            raise
        return page

    return toolset
