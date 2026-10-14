"""Dormant categories — the two halves of a group the model loads for itself.

An expensive category the average turn never opens is still paid for on every request:
its schemas ride in the prompt whether or not a page is loaded or an inbox is read. The
browser alone outweighs the standing brief. Pydantic AI's deferred loading is the answer
to that, and it needs exactly two things from us that the library cannot supply:

- **a way in** — ``tool_search_capability``: the ``ToolSearch`` capability, configured
  with a strategy that understands *groups*. The library's own keyword overlap ranks
  individual tools, which is the wrong unit here: a turn that decides it needs the
  browser needs the whole browser, not the four ``browse_*`` tools whose descriptions
  happen to share a word with the query. Naming a dormant category reveals all of it;
  anything else falls back to keyword matching, so a query like "send an email" still
  finds its way in without the model knowing the group's name.
- **a reason to look** — ``dormant_index_instructions``: one line per dormant group in
  the standing brief. Deferred loading is invisible by construction; a model that is
  never told the browser exists will never search for it, and will instead say it
  cannot open a page. The index is what makes the saving safe, and it is deliberately
  the cheapest possible form of it — a name and a summary, no schemas, no counts that
  churn from turn to turn and cost the prompt-prefix cache the whole history behind it.

Nothing here decides *what* to load. The chassis withholds and the model reveals; there
is no relevance heuristic anywhere in the path, which is the same posture the enabled
gate takes about tools generally (``toolsets.py``).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

from pydantic_ai import RunContext, ToolDefinition
from pydantic_ai.capabilities import ToolSearch

from .deps import AsyncInstructionProvider, RunDeps
from .describe import category_of

__all__ = ["dormant_index_instructions", "tool_search_capability"]

_TOKEN = re.compile(r"[a-z0-9]+")

#: The library's own default, kept as a floor: a keyword fallback that found fewer
#: matches than the stock search would have is a regression dressed as a feature.
_MIN_RESULTS = 10


def _tokens(text: str) -> list[str]:
    return _TOKEN.findall(text.lower())


def _named_categories(query: str, categories: Sequence[str]) -> list[str]:
    """Which dormant groups a query names outright.

    A group is named by any token that starts with its own name, which is what makes
    ``browse``, ``browse_click`` and "browser tools" all resolve to the same group
    without a table of synonyms — the model writes the word it is thinking of, and the
    prefix is the part it cannot get wrong. Longest name first, so a category whose name
    prefixes another's cannot swallow the other's queries.
    """
    tokens = _tokens(query)
    return [
        category
        for category in sorted(categories, key=len, reverse=True)
        if any(token.startswith(category) for token in tokens)
    ]


def tool_search_capability(
    names_by_category: Mapping[str, frozenset[str]],
    dormant: Mapping[str, str],
) -> ToolSearch[RunDeps]:
    """The discovery surface for the dormant groups — group-aware, not tool-aware.

    ``dormant`` is category → summary, assembled from the manifests;
    ``names_by_category`` is the catalog's own membership map (``describe.category_names``),
    read only to size the result cap. The capability is returned even when nothing is
    dormant: with no deferred tool, Pydantic AI never emits ``search_tools`` and never
    puts the builtin on the wire, so an installation whose dormant features are all
    switched off pays nothing for the seam being here.

    ``max_results`` is raised to cover every dormant tool at once. The default of ten
    would truncate a browser reveal halfway through and hand the model a category it
    can only half use — the one failure this design cannot tolerate, since the point of
    naming a group is that the group arrives whole.
    """
    names = tuple(dormant)
    # The membership map narrowed to the dormant groups, so the one longest-prefix rule
    # in `describe` resolves a namespaced name here too — a second copy of it would drift
    # from the one the descriptions and the operator's catalog are built against.
    dormant_names = {category: names_by_category.get(category, frozenset()) for category in names}
    dormant_tool_count = sum(len(members) for members in dormant_names.values())

    def _search(
        _ctx: RunContext[RunDeps],
        queries: Sequence[str],
        tools: Sequence[ToolDefinition],
    ) -> list[str]:
        """Group first, keywords second, in the order the model asked."""
        by_category: dict[str, list[str]] = {}
        for tool_def in tools:
            category = category_of(tool_def.name, dormant_names)
            if category is not None:
                by_category.setdefault(category, []).append(tool_def.name)

        found: list[str] = []
        seen: set[str] = set()

        def _take(name: str) -> None:
            if name not in seen:
                seen.add(name)
                found.append(name)

        for query in queries:
            for category in _named_categories(query, names):
                for name in by_category.get(category, ()):
                    _take(name)
        # Only what naming a group did not already answer: a query that hit a category
        # has been served in full, and re-ranking its tools by word overlap would just
        # reorder them.
        terms = {term for query in queries for term in _tokens(query)}
        if terms:
            scored = [
                (len(terms & set(_tokens(f"{t.name} {t.description or ''}"))), t.name)
                for t in tools
                if t.name not in seen
            ]
            for _, name in sorted(((score, name) for score, name in scored if score), reverse=True):
                _take(name)
        return found

    return ToolSearch[RunDeps](
        strategy=_search,
        max_results=max(_MIN_RESULTS, dormant_tool_count),
        tool_description=_TOOL_DESCRIPTION if names else None,
        parameter_description=_QUERIES_DESCRIPTION if names else None,
    )


#: What ``search_tools`` does, and nothing else. The roster, the fact that a loaded group
#: stays loaded, and the rule about loading before refusing are all in the standing index
#: — one copy each, and the index is the thing the model reads *before* it knows there is
#: a tool to call.
_TOOL_DESCRIPTION = "Load tools that are not currently in your list."

#: The library's default here tells the model to use "specific words likely to appear in
#: tool names or descriptions" — the one thing it cannot do, since a dormant group's names
#: and descriptions are exactly what has been withheld from it. This says what actually
#: resolves: the group name first, a description of the work as the fallback.
_QUERIES_DESCRIPTION = (
    "A group's name loads every tool in that group. When you do not know which group "
    "holds what you need, describe the work instead and the closest tools are loaded."
)


def dormant_index_instructions(
    dormant: Mapping[str, str],
    names_by_category: Mapping[str, frozenset[str]],
) -> AsyncInstructionProvider:
    """The standing one-line index of the dormant groups.

    Built as a factory over the assembled declarations because a provider is handed
    nothing but the run context, and what is dormant is an app-assembly fact rather than
    a per-run one. The provider it returns is named for the slug its contribution is
    filed under (``agent/injections.contributor_id``), so the composition readout and the
    work log both call this block *dormant*.

    A group every one of whose tools the operator disabled — or that offline mode or a
    missing configuration has withheld — is dropped from the index entirely. Advertising
    a group whose reveal can only come back empty spends the model a round trip to learn
    that a capability it was just told about does not exist.
    """
    prefixed = {
        category: frozenset(
            f"{category}_{name}" for name in names_by_category.get(category, frozenset())
        )
        for category in dormant
    }

    async def dormant_instructions(ctx: RunContext[RunDeps]) -> str:
        disabled = ctx.deps.disabled_tools
        lines = [
            f"- {category}: {summary}"
            for category, summary in dormant.items()
            if prefixed.get(category) and not prefixed[category] <= disabled
        ]
        if not lines:
            return ""
        return _INDEX.format(groups="\n".join(lines))

    return dormant_instructions


#: Kept here rather than in ``prompts/`` because it is not a standing instruction: it
#: describes a mechanism this module implements, and every word of it would have to
#: change if the mechanism did.
#:
#: **It says what a group *can* do, never what is currently loaded.** The block is
#: byte-stable across the conversation so the prompt-prefix cache survives, which means
#: it is re-sent unchanged after a reveal — and a line asserting the browser is not
#: loaded, sent alongside eighteen browser tools, is read and acted on: the model either
#: searches for what it already holds or tells the operator it cannot open a page.
_INDEX = """\
These tool groups can be loaded on demand. A group's tools are in your list only once \
you have loaded it, which is what keeps this conversation small:

{groups}

Call search_tools with a group's name to load it. The whole group arrives at once and \
stays for the rest of the conversation. Load a group when the work needs it, instead of \
telling the operator there is no way to do something."""
