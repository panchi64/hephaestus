"""Web search over a SearXNG instance — the agent's window onto the open web.

By default the backend's own managed instance (`services.searxng`), needing no operator
setup; an enabled provider in the DB-backed registry overrides it (a custom/remote
instance). **search** queries the active instance's JSON API and returns the hits (title,
url, snippet) as a :class:`SearchResults` batch: the untrusted-content preamble ships once
(:func:`core.untrusted.untrusted_preamble`) and each snippet is a bare fence sharing that
call's one nonce (:func:`core.untrusted.untrusted_fence`) — web content is data, never
instructions, but the "treat as data" instruction needn't repeat per result. The provider
catalog is owner-scoped and managed like the model registry (encrypted key seam,
``in_session`` writes). The service raises domain errors only; the tool/route layers map
them to retries or HTTP, keeping it reusable by non-agent callers.

Fetching a page's *content* is a separate capability — :mod:`services.webfetch` renders
the page in a headless browser and extracts it to Markdown.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy import Engine
from sqlmodel import Session, select

from core.citations import Citation
from core.db import get_owned, in_session
from core.exceptions import DegradedCapabilityError
from core.untrusted import unfence, untrusted_fence, untrusted_preamble
from core.vault import Vault
from models.search import SearchProvider


@dataclass(frozen=True)
class SearchResult:
    """One web search hit. ``snippet`` is untrusted-wrapped (the provider relays
    text from arbitrary pages); ``title``/``url`` are short structural metadata.
    ``published`` is the raw publication date the engine reported (``publishedDate``),
    when it knew one — a bare string, useful for judging freshness."""

    title: str
    url: str
    snippet: str
    published: str | None = None


@dataclass(frozen=True)
class SearchResults:
    """A whole search call's results. ``instruction`` is the single untrusted-content
    preamble for the batch (``""`` when there were no hits); each result's ``snippet`` is
    a bare fence sharing that call's one nonce — so a batch of N results carries the
    "treat as data" instruction once, not once per snippet."""

    instruction: str
    results: list[SearchResult]

    def citations(self) -> list[Citation]:
        """The hits, in result order — how the run stream learns a search's sources
        without the event translator having to know what a search result is. Already
        URL-unique from the service, so nothing is deduped here.

        ``listed`` and not a stronger claim: a search hit is a title, a URL and whatever
        the engine chose to show of the page. The page itself was not opened, and a row
        that said otherwise would make "cited" mean no more than "an engine returned it".
        A `web_fetch` of one of these emits it again at ``read``.

        The snippet is **unfenced** here. The fence is how the text is handed to the
        model; the citation is how it is handed to the operator, and a row opening on
        ``[BEGIN UNTRUSTED CONTENT …]`` is marker noise in front of the sentence they
        wanted. Nothing is more trusted for the unwrap — it never leaves this row.

        **A hit with no URL is dropped rather than cited.** The parse above keeps such a
        hit deliberately — ``if url and url in seen`` never dedups the empty string, so
        every URL-less result an engine returns is passed to the model, which can still
        use the snippet. A citation cannot survive the same way: it is a row the operator
        clicks, :class:`~core.citations.Citation` refuses a web source without an address,
        and letting one through would raise here — inside the translator's citation pass,
        taking every *other* source in the same search down with it.
        """
        return [
            Citation(
                url=item.url,
                title=item.title,
                snippet=unfence(item.snippet),
                published=item.published,
                engagement="listed",
            )
            for item in self.results
            if isinstance(item, SearchResult) and item.url
        ]


@dataclass(frozen=True)
class _SearchTarget:
    """The resolved instance to query — either an operator-configured provider or
    the backend's managed SearXNG. ``api_key`` is already decrypted."""

    base_url: str
    engines: list[str]
    params: dict[str, Any]
    api_key: str | None


class SearchService:
    """SearXNG-backed web search + the owner-scoped provider catalog.

    ``http_client`` is the pooled outbound client (``follow_redirects=False`` — an
    unguarded redirect off the JSON API would be an SSRF hole, so search refuses to follow
    one); None ⇒ a transient client per call (the path tests take). ``managed_url`` returns
    the backend's self-managed SearXNG URL (or ``None`` until it is ready) — the zero-config
    default used when the operator has configured no provider of their own.
    """

    def __init__(
        self,
        engine: Engine,
        vault: Vault,
        *,
        http_client: httpx.AsyncClient | None = None,
        managed_url: Callable[[], str | None] | None = None,
        timeout_s: float = 15.0,
        result_limit: int = 10,
    ) -> None:
        self._engine = engine
        self._vault = vault
        self._http_client = http_client
        self._managed_url = managed_url
        self._timeout_s = timeout_s
        self._result_limit = result_limit

    # --- provider catalog -------------------------------------------------

    async def list_providers(self, owner_id: str) -> list[SearchProvider]:
        def work(session: Session) -> list[SearchProvider]:
            return list(
                session.exec(
                    select(SearchProvider)
                    .where(SearchProvider.owner_id == owner_id)
                    .order_by(SearchProvider.name)
                ).all()
            )

        return await in_session(self._engine, work)

    async def get_provider(self, owner_id: str, provider_id: str) -> SearchProvider:
        return await get_owned(
            self._engine, SearchProvider, provider_id, owner_id, what="search provider"
        )

    async def create_provider(
        self,
        owner_id: str,
        *,
        name: str,
        base_url: str,
        enabled: bool = True,
        engines: list[str] | None = None,
        params: dict[str, Any] | None = None,
        api_key: str | None = None,
    ) -> SearchProvider:
        provider = SearchProvider(
            owner_id=owner_id,
            name=name,
            base_url=base_url,
            enabled=enabled,
            engines=engines or [],
            params=params or {},
            api_key_enc=self._vault.encrypt_str(api_key) if api_key else None,
        )

        def work(session: Session) -> SearchProvider:
            session.add(provider)
            session.flush()
            session.refresh(provider)
            return provider

        return await in_session(self._engine, work)

    async def update_provider(
        self, owner_id: str, provider_id: str, **changes: object
    ) -> SearchProvider:
        """Apply field changes. ``api_key`` (plaintext, or "" to clear) is sealed
        before storage; every other key maps straight onto the column."""
        await self.get_provider(owner_id, provider_id)  # ownership check

        def work(session: Session) -> SearchProvider:
            provider = session.get(SearchProvider, provider_id)
            assert provider is not None  # just confirmed it exists and is owned
            for key, value in changes.items():
                if key == "api_key":
                    provider.api_key_enc = self._vault.encrypt_str(str(value)) if value else None
                elif value is not None:
                    setattr(provider, key, value)
            provider.updated_at = datetime.now(UTC)
            session.add(provider)
            session.flush()
            session.refresh(provider)
            return provider

        return await in_session(self._engine, work)

    async def delete_provider(self, owner_id: str, provider_id: str) -> None:
        await self.get_provider(owner_id, provider_id)  # ownership check

        def work(session: Session) -> None:
            provider = session.get(SearchProvider, provider_id)
            if provider is not None:
                session.delete(provider)

        await in_session(self._engine, work)

    async def _resolve_target(self, owner_id: str) -> _SearchTarget:
        """The instance to query: the first enabled operator-configured provider
        (an override), else the backend's managed SearXNG, else a degraded
        capability (no web search)."""
        providers = await self.list_providers(owner_id)
        active = next((p for p in providers if p.enabled), None)
        if active is not None:
            return _SearchTarget(
                base_url=active.base_url,
                engines=active.engines,
                params=active.params,
                api_key=self._vault.decrypt_str(active.api_key_enc) if active.api_key_enc else None,
            )
        managed = self._managed_url() if self._managed_url is not None else None
        if managed:
            return _SearchTarget(base_url=managed, engines=[], params={}, api_key=None)
        raise DegradedCapabilityError("no web search provider configured")

    # --- search -----------------------------------------------------------

    async def search(
        self,
        owner_id: str,
        query: str,
        *,
        limit: int | None = None,
        time_range: str | None = None,
    ) -> SearchResults:
        """Query the active SearXNG provider's JSON API. An empty result list is a
        valid answer (the model concludes, rather than looping); an unreachable
        provider or non-JSON response is a degraded capability. ``time_range``, when one
        of ``day``/``week``/``month``/``year``, restricts results to recent pages (any
        other value is ignored)."""
        target = await self._resolve_target(owner_id)
        limit = self._result_limit if limit is None else limit
        params: dict[str, Any] = {"q": query, "format": "json", **target.params}
        if target.engines:
            params["engines"] = ",".join(target.engines)
        if time_range in ("day", "week", "month", "year"):
            params["time_range"] = time_range
        headers: dict[str, str] = {}
        if target.api_key:
            headers["Authorization"] = f"Bearer {target.api_key}"
        url = target.base_url.rstrip("/") + "/search"

        client = self._http_client or httpx.AsyncClient()
        owns = self._http_client is None
        try:
            # No redirect-following: the provider answers /search?format=json
            # directly, and an unguarded redirect would be an SSRF hole — so refuse it.
            resp = await client.get(
                url, params=params, headers=headers, timeout=self._timeout_s, follow_redirects=False
            )
        except httpx.HTTPError as exc:
            raise DegradedCapabilityError(f"search provider unreachable: {exc}") from exc
        finally:
            if owns:
                await client.aclose()
        if resp.status_code >= 400 or resp.is_redirect:
            raise DegradedCapabilityError(f"search provider returned HTTP {resp.status_code}")
        try:
            data = resp.json()
        except ValueError as exc:
            raise DegradedCapabilityError(
                "search provider did not return JSON (enable SearXNG's json format)"
            ) from exc
        if not isinstance(data, dict):
            raise DegradedCapabilityError("search provider returned an unexpected JSON shape")

        results = data.get("results") or []
        # One nonce for the whole call: the preamble ships once and every snippet is a bare
        # fence sharing it, so a batch of N results doesn't repeat the "treat as data"
        # instruction N times.
        nonce = secrets.token_hex(8)
        # Dedupe exact-URL repeats (two engines agreeing) *before* the limit, so the cap
        # yields `limit` distinct pages rather than being spent on duplicates.
        seen: set[str] = set()
        out: list[SearchResult] = []
        for r in results:
            url = r.get("url") or ""
            if url and url in seen:
                continue
            seen.add(url)
            out.append(
                SearchResult(
                    title=r.get("title") or "",
                    url=url,
                    snippet=untrusted_fence(r.get("content") or "", nonce, source=url),
                    published=r.get("publishedDate") or None,
                )
            )
            if len(out) >= limit:
                break
        instruction = untrusted_preamble(nonce) if out else ""
        return SearchResults(instruction=instruction, results=out)
