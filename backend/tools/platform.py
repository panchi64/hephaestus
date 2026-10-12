"""Platform search — the agent's one way to reach anything outside its own conversation.

A conversation's context holds only that conversation. Everything else the platform has
accumulated (other conversations, memory, indexed files and uploads, thread work
summaries, sub-agent reports, routine outputs, and the skill and agent catalogs) is
searched here on demand instead of being injected, so the brief does not grow with the
platform's history.

``platform_search`` fans out over the corpus sources and the catalogs and fuses them by
rank. Each hit carries a ``ref``; ``platform_read`` opens one in full. Searching sources
that hold other people's words is global recall and pauses for approval (AE-3.8); memory
and the catalogs hold only what the operator kept or approved, so a search confined to
them does not.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from pydantic_ai import FunctionToolset, ModelRetry, RunContext

from core.citations import Citation
from core.untrusted import new_nonce, unfence, untrusted_fence, untrusted_preamble
from services import ranking
from services.corpus import CorpusHit, CorpusIndex
from services.plugins import PluginStore
from services.projects import visible_project_ids
from services.recall_index import rank_texts
from services.skills import SkillStore

from .deps import RunDeps
from .project_agents import run_catalog
from .recall_gate import gate_global_recall

Source = Literal[
    "conversations",
    "memory",
    "files",
    "summaries",
    "reports",
    "routines",
    "skills",
    "agents",
    "plugins",
]

_CORPUS_KINDS: dict[str, tuple[str, ...]] = {
    "conversations": ("conversations",),
    "memory": ("memory",),
    "files": ("folder", "uploads"),
    "summaries": ("summaries",),
    "reports": ("reports",),
    "routines": ("routines",),
}
_SOURCE_OF_SURFACE = {
    "surf-conversations": "conversations",
    "surf-memory": "memory",
    "surf-summaries": "summaries",
    "surf-reports": "reports",
    "surf-routines": "routines",
}
#: Surfaces keyed by conversation id, which never return the caller's own thread.
_THREAD_SURFACES = frozenset({"surf-conversations", "surf-summaries"})
#: Sources answered from a catalog of what the operator published, not from the corpus.
_CATALOGS: frozenset[str] = frozenset({"skills", "agents", "plugins"})
_UNGATED: frozenset[str] = frozenset({"memory"}) | _CATALOGS
_ALL: tuple[Source, ...] = (
    "conversations",
    "memory",
    "files",
    "summaries",
    "reports",
    "routines",
    "skills",
    "agents",
    "plugins",
)
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
            f"`since` must be an ISO date or datetime such as 2026-03-01, not {value!r}."
        ) from None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _stamp(at: datetime | None) -> str | None:
    return at.isoformat(timespec="minutes") if at is not None else None


def _source_of(hit: CorpusHit) -> str:
    return _SOURCE_OF_SURFACE.get(hit.source_id, "files")


def platform_toolset() -> FunctionToolset[RunDeps]:
    toolset: FunctionToolset[RunDeps] = FunctionToolset()

    @toolset.tool
    async def search(
        ctx: RunContext[RunDeps],
        query: str,
        sources: list[Source] | None = None,
        since: str | None = None,
        limit: int = 8,
    ) -> PlatformResults | str:
        """Search everything outside this conversation: other conversations, memory,
        files and uploads, work summaries, agent reports, routine outputs, skills, agents,
        plugins.

        Use it before saying you don't know, or asking what the operator may have told
        you before. ``since`` is an ISO date. Open a hit's ``ref`` with
        ``platform_read``. No hits means nothing matched; don't keep rephrasing."""
        chosen = tuple(dict.fromkeys(sources or _ALL))
        after = _since(since)
        if not set(chosen) <= _UNGATED:
            gate_global_recall(ctx)
        deps = ctx.deps
        lists: list[list[PlatformHit]] = []
        nonce = new_nonce()

        kinds = [kind for source in chosen for kind in _CORPUS_KINDS.get(source, ())]
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
        """Open a ``platform_search`` hit in full by its ``ref``."""
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
    store = ctx.deps.caps.get_optional(SkillStore)
    if store is None:
        return []
    entries = {entry.name: entry for entry in await store.catalog(ctx.deps.owner_id)}
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
