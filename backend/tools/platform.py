"""Platform search — the agent's one way to reach anything outside its own conversation.

A conversation's context holds only that conversation. Everything else the platform has
accumulated (other conversations, memory, indexed files and uploads, thread work
summaries, sub-agent reports, whatever a feature indexes, and the skill and agent
catalogs) is searched here on demand instead of being injected, so the brief does not
grow with the platform's history.

``platform_search`` fans out over the corpus sources and the catalogs and fuses them by
rank. Each hit carries a ``ref``; ``platform_read`` opens one in full. Searching sources
that hold other people's words is global recall and pauses for approval (AE-3.8); memory
and the catalogs hold only what the operator kept or approved, so a search confined to
them does not.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import cast

from pydantic_ai import FunctionToolset, ModelRetry, RunContext
from pydantic_ai.tools import ToolDefinition

from core.citations import Citation
from core.declarations import DeclaredTable
from core.diagnostics import render_tool_error
from core.untrusted import new_nonce, unfence, untrusted_fence, untrusted_preamble
from services import ranking
from services.corpus import CorpusHit, CorpusIndex
from services.plugins import PluginStore
from services.projects import visible_project_ids
from services.recall_index import rank_texts

from .deps import RunDeps
from .project_agents import run_catalog
from .recall_gate import gate_global_recall
from .skills import skill_catalog


@dataclass(frozen=True)
class PlatformSource:
    """A corpus-backed source ``platform_search`` can be pointed at by name."""

    #: The corpus source kinds searched for it.
    corpus_kinds: tuple[str, ...]
    #: The corpus surface whose hits are reported as this source; ``None`` for one made of
    #: indexed folders and uploads.
    surface_id: str | None = None


#: Source name → what it searches. A feature indexing its own records into the corpus
#: declares its source on its manifest (``FeatureManifest.platform_sources``).
PLATFORM_SOURCES: DeclaredTable[str, PlatformSource] = DeclaredTable(
    "platform sources",
    {
        "conversations": PlatformSource(("conversations",), "surf-conversations"),
        "memory": PlatformSource(("memory",), "surf-memory"),
        "files": PlatformSource(("folder", "uploads")),
        "summaries": PlatformSource(("summaries",), "surf-summaries"),
        "reports": PlatformSource(("reports",), "surf-reports"),
    },
)
#: Surfaces keyed by conversation id, which never return the caller's own thread.
_THREAD_SURFACES = frozenset({"surf-conversations", "surf-summaries"})
#: Sources answered from a catalog of what the operator published, not from the corpus.
_CATALOGS: frozenset[str] = frozenset({"skills", "agents", "plugins"})
_UNGATED: frozenset[str] = frozenset({"memory"}) | _CATALOGS


def all_sources() -> tuple[str, ...]:
    return (*PLATFORM_SOURCES, *sorted(_CATALOGS))


#: Characters of one item ``platform_read`` returns before cutting it short.
READ_LIMIT = 40_000


@dataclass(frozen=True)
class PlatformHit:
    ref: str
    source: str
    title: str
    #: The excerpt, inside this batch's untrusted fence (catalog entries are not fenced).
    excerpt: str
    at: str | None
    matched_by: str


@dataclass(frozen=True)
class PlatformResults:
    instruction: str
    hits: list[PlatformHit]

    def citations(self) -> list[Citation]:
        return [
            Citation(
                kind="corpus",
                title=hit.title,
                source_id=hit.ref.partition(":")[0],
                ref=hit.ref.partition(":")[2],
                snippet=unfence(hit.excerpt),
                engagement="listed",
            )
            for hit in self.hits
            if hit.source not in _CATALOGS
        ]


@dataclass(frozen=True)
class PlatformDocument:
    ref: str
    title: str
    instruction: str
    text: str
    at: str | None = None

    def citations(self) -> list[Citation]:
        if self.ref.startswith(("skill:", "agent:", "plugin:")):
            return []
        source_id, _, ref = self.ref.partition(":")
        return [
            Citation(
                kind="corpus",
                title=self.title,
                source_id=source_id,
                ref=ref,
                snippet=unfence(self.text),
                engagement="read",
            )
        ]


def _since(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ModelRetry(
            render_tool_error(
                f"`since` is not an ISO date: {value!r}",
                hints=["pass a date such as `2026-03-01`, or a datetime"],
            )
        ) from None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _stamp(at: datetime | None) -> str | None:
    return at.isoformat(timespec="minutes") if at is not None else None


def _source_of(hit: CorpusHit) -> str:
    return next(
        (name for name in PLATFORM_SOURCES if PLATFORM_SOURCES[name].surface_id == hit.source_id),
        "files",
    )


async def _enumerate_sources(_ctx: RunContext[RunDeps], tool_def: ToolDefinition) -> ToolDefinition:
    """Name every source in the schema; the set is declared by features, so it can't be a
    ``Literal`` in the signature."""
    schema = dict(tool_def.parameters_json_schema)
    properties = dict(cast(dict[str, object], schema.get("properties", {})))
    properties["sources"] = {
        "anyOf": [
            {"type": "array", "items": {"type": "string", "enum": list(all_sources())}},
            {"type": "null"},
        ],
        "default": None,
    }
    schema["properties"] = properties
    return replace(tool_def, parameters_json_schema=schema)


def platform_toolset() -> FunctionToolset[RunDeps]:
    toolset: FunctionToolset[RunDeps] = FunctionToolset()

    @toolset.tool(prepare=_enumerate_sources)
    async def search(
        ctx: RunContext[RunDeps],
        query: str,
        sources: list[str] | None = None,
        since: str | None = None,
        limit: int = 8,
    ) -> PlatformResults | str:
        """Searches everything the platform holds outside this conversation: other
        conversations, memory, the operator's indexed files and uploads, work summaries,
        sub-agent reports, what features such as routines index, and the catalogs of
        skills, agents and plugins. Your context holds only this conversation, so this is
        how you reach the rest.

        Use it before you say you don't know something, and before you ask the operator
        anything they may have told you already. Use `web_search` for the public web
        instead, and `corpus_retrieve` to search inside a file you already have the id of.

        Returns up to `limit` hits ranked across sources, each with a `ref`, its
        `source`, a title, an excerpt and a date. The excerpt is a fragment: open a hit
        with `platform_read` before you rely on it or quote it. No hits means nothing
        matched; conclude from that rather than rephrasing the same search.

        A search confined to `memory` and the catalogs runs at once. Any other source
        holds content the operator did not write, so the search pauses for their
        approval first; name only the `sources` you need.

        Args:
            query: What you are looking for, in plain words. It matches by meaning and by
                keyword.
            sources: Which sources to search. Omit to search all of them.
            since: An ISO date (`2026-03-01`) or datetime; only items from then on are
                returned. The catalogs carry no dates, so they are skipped when this is
                set.
            limit: The most hits to return. Default 8.
        """
        known = all_sources()
        chosen = tuple(dict.fromkeys(sources or known))
        unknown = [source for source in chosen if source not in known]
        if unknown:
            raise ModelRetry(
                f"Unknown source(s) {', '.join(unknown)}; choose from {', '.join(known)}."
            )
        after = _since(since)
        if not set(chosen) <= _UNGATED:
            gate_global_recall(ctx)
        deps = ctx.deps
        lists: list[list[PlatformHit]] = []
        nonce = new_nonce()

        kinds = [
            kind
            for source in chosen
            if (spec := PLATFORM_SOURCES.get(source)) is not None
            for kind in spec.corpus_kinds
        ]
        index = deps.caps.get_optional(CorpusIndex)
        if kinds and index is None and not _CATALOGS & set(chosen):
            return "Platform search is unavailable."
        if kinds and index is not None:
            hits = await index.retrieve(
                deps.owner_id,
                query,
                sources=kinds,
                limit=limit + 1,
                visible_projects=visible_project_ids(deps.project_id),
                since=after,
            )
            lists.append(
                [
                    PlatformHit(
                        ref=hit.gid,
                        source=_source_of(hit),
                        title=hit.title or hit.ref,
                        excerpt=untrusted_fence(hit.text, nonce, source=hit.gid),
                        at=_stamp(hit.at),
                        matched_by=hit.matched_by,
                    )
                    for hit in hits
                    if not (hit.source_id in _THREAD_SURFACES and hit.ref == deps.conversation_id)
                ]
            )
        tokens = ranking.tokens(query)
        if "skills" in chosen and after is None:
            lists.append(await _skill_hits(ctx, tokens))
        if "agents" in chosen and after is None:
            lists.append(await _agent_hits(ctx, tokens))
        if "plugins" in chosen and after is None:
            lists.append(await _plugin_hits(ctx, tokens))

        by_ref = {hit.ref: hit for hits in lists for hit in hits}
        fused = ranking.rrf_lists([[hit.ref for hit in hits] for hits in lists])
        ranked = sorted(fused.items(), key=lambda kv: kv[1], reverse=True)[:limit]
        found = [by_ref[ref] for ref, _ in ranked]
        fenced = any(hit.source not in _CATALOGS for hit in found)
        return PlatformResults(instruction=untrusted_preamble(nonce) if fenced else "", hits=found)

    @toolset.tool
    async def read(ctx: RunContext[RunDeps], ref: str) -> PlatformDocument | str:
        """Opens one `platform_search` hit in full by its `ref`: a past conversation, a
        memory, a file, a summary, a report or a routine's output, cut off after
        40,000 characters.

        A skill or plugin hit is not opened here; the result tells you which tool loads
        it. An agent hit returns the agent's description and instructions.

        Args:
            ref: The `ref` exactly as the search hit gave it.
        """
        deps = ctx.deps
        kind, _, name = ref.partition(":")
        if kind == "skill":
            return (
                f"`{name}` is a skill. Call `skills_open` with that name to load its instructions."
            )
        if kind == "plugin":
            return (
                f"`{name}` is a plugin. Load its tools with `search_tools` for the `plugins` "
                f"group; they are named `plugins_{name}_<tool>`."
            )
        if kind == "agent":
            agent = (await run_catalog(ctx)).get(name)
            if agent is None:
                return f"No agent named {name!r}."
            return PlatformDocument(
                ref=ref,
                title=agent.name,
                instruction="",
                text=f"{agent.description}\n\n{agent.instructions}".strip(),
            )
        index = deps.caps.get_optional(CorpusIndex)
        if index is None:
            return "Platform search is unavailable."
        document = await index.read(
            deps.owner_id, ref, visible_projects=visible_project_ids(deps.project_id)
        )
        if document is None:
            return f"Nothing found for ref {ref!r}. Use a `ref` from a `platform_search` hit."
        text = document.text
        if len(text) > READ_LIMIT:
            text = (
                f"{text[:READ_LIMIT]}\n\n[Cut at {READ_LIMIT} characters; "
                f"{len(text) - READ_LIMIT} more not shown.]"
            )
        nonce = new_nonce()
        return PlatformDocument(
            ref=ref,
            title=document.title,
            instruction=untrusted_preamble(nonce),
            text=untrusted_fence(text, nonce, source=ref),
            at=_stamp(document.at),
        )

    return toolset


async def _skill_hits(ctx: RunContext[RunDeps], tokens: set[str]) -> list[PlatformHit]:
    entries = {entry.name: entry for entry in await skill_catalog(ctx)}
    ranked = rank_texts(
        {name: f"{name} {entry.description}" for name, entry in entries.items()},
        tokens,
    )
    return [
        PlatformHit(
            ref=f"skill:{name}",
            source="skills",
            title=name,
            excerpt=entries[name].description,
            at=None,
            matched_by="keyword",
        )
        for name, _ in ranked
    ]


async def _agent_hits(ctx: RunContext[RunDeps], tokens: set[str]) -> list[PlatformHit]:
    roster = await run_catalog(ctx)
    ranked = rank_texts(
        {name: f"{name} {agent.description}" for name, agent in roster.items()},
        tokens,
    )
    return [
        PlatformHit(
            ref=f"agent:{name}",
            source="agents",
            title=name,
            excerpt=roster[name].description,
            at=None,
            matched_by="keyword",
        )
        for name, _ in ranked
    ]


async def _plugin_hits(ctx: RunContext[RunDeps], tokens: set[str]) -> list[PlatformHit]:
    store = ctx.deps.caps.get_optional(PluginStore)
    if store is None:
        return []
    manifests = {
        view.name: view.active
        for view, _files in await store.published(ctx.deps.owner_id)
        if view.active is not None
    }
    ranked = rank_texts(
        {
            name: " ".join([name, m.description, *(t.description for t in m.tools)])
            for name, m in manifests.items()
        },
        tokens,
    )
    return [
        PlatformHit(
            ref=f"plugin:{name}",
            source="plugins",
            title=name,
            excerpt=f"{manifests[name].description} Tools: "
            + ", ".join(f"plugins_{name}_{tool.name}" for tool in manifests[name].tools),
            at=None,
            matched_by="keyword",
        )
        for name, _ in ranked
    ]
