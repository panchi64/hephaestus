"""The operator-facing tool catalog — every tool, named exactly as the agent sees it.

`AE-3.3` lets the operator disable individual tools, which means the settings surface has
to *list* them. That list is **derived from the same assembled category mapping the agent
runs against** (core categories + every feature manifest's ``toolsets`` export, assembled
at app startup), never hand-maintained: a tool that lands in a category appears here the
day it lands, and one that is renamed or removed cannot leave a stale row behind that
disables nothing. Callers pass that mapping in — this module holds no registry of its own.

Names are namespaced the way :meth:`AbstractToolset.prefixed` namespaces them —
``f"{category}_{tool}"`` — because that is the name the enabled gate matches on and the
name the model is offered. ``tests/test_tool_policy.py`` pins that convention against the
tool definitions a real agent run resolves, so a change in the library's prefixing is a
failing test rather than a settings screen whose toggles quietly stop applying.

**Scope.** Every category today is a Pydantic AI ``FunctionToolset``, whose tools are
known statically. A future toolset that resolves its tools dynamically (an external MCP
server, `MCP-*`) has nothing to enumerate here and contributes no rows — such tools carry
their own operator-facing trust surface (`AE-3.6`).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from pydantic_ai import AbstractToolset

from services.approval_grants import ONCE_ONLY_TOOLS
from services.external_tools import ExternalTools

from .deps import RunDeps
from .describe import category_names, describe_text, registered_tools


@dataclass(frozen=True)
class ToolInfo:
    """One tool as the operator sees it on the settings surface."""

    # The namespaced name — what the enabled gate matches and what the model is offered.
    name: str
    # The category it was registered under, so the surface can group by cluster.
    category: str
    # The tool's own description (its docstring) — what the model is told it does, which
    # is also the most honest thing to show the operator deciding whether to allow it.
    description: str
    # Whether its category starts each conversation dormant — registered and allowed, but
    # with its schemas withheld until the model asks for the group by name. Reported
    # rather than folded into `enabled` for the reason the offline suspension is kept out
    # of that flag too: this is not the operator's choice and there is nothing here for
    # them to act on, but a tool the agent has to reach for before it can use is a
    # different row from one that is simply there.
    dormant: bool = False


_NO_CATEGORIES: frozenset[str] = frozenset()


def _flatten(text: str) -> str:
    """A description is a docstring, so its hard wrapping is an artifact of the source
    file rather than meaning. Collapse it to flowing text and let the surface lay it out;
    the whole description is kept — the operator deciding whether to allow a tool deserves
    everything the model was told about it."""
    return " ".join(text.split())


def tool_catalog(
    categories: Mapping[str, AbstractToolset[RunDeps]],
    dormant_categories: frozenset[str] = _NO_CATEGORIES,
) -> list[ToolInfo]:
    """Every registered tool, category-then-name ordered for a stable listing.

    ``categories`` is the assembled mapping the agent itself runs against (the routes
    read it off ``app.state``), so the listing and the agent's stack cannot diverge.
    ``dormant_categories`` is the assembled dormant set, passed in the same way
    ``gated_tools`` is — the declaration lives on the manifests, and this module keeps
    no registry of its own.

    Descriptions go through the same rewrite the agent's own stack applies
    (``describe.py``) — the operator deciding whether to allow a tool should be reading
    the text the model was handed, not the raw docstring with its library scaffolding
    and its references to names nothing is registered under. The rewrite is a pure
    function over the description string; the toolsets' own tool objects are untouched.
    """
    names = category_names(categories)
    catalog = [
        ToolInfo(
            name=f"{category}_{tool_name}",
            category=category,
            description=_flatten(
                describe_text(f"{category}_{tool_name}", tool.description, names) or ""
            ),
            dormant=category in dormant_categories,
        )
        # A non-function toolset exposes no static tool registry; it contributes nothing
        # rather than breaking the listing (see the module docstring).
        for category, toolset in categories.items()
        for tool_name, tool in sorted(registered_tools(toolset).items(), key=lambda kv: kv[0])
    ]
    return sorted(catalog, key=lambda t: (t.category, t.name))


async def approval_scopes(
    external: ExternalTools | None,
    owner_id: str,
    categories: Mapping[str, AbstractToolset[RunDeps]],
    gated_tools: frozenset[str],
) -> list[ToolInfo]:
    """Every tool that can pause a run for approval — the vocabulary a conversation grant
    (`AE-3.7`) or a scheduled task's pre-authorization (`AE-3.5`) may name.

    This **cannot** be a constant. External tools are named ``external_{slug}_{tool}``
    from the operator's own registered servers and configured connectors, so the set
    changes when they register one — a hand-maintained list would reject a perfectly
    valid scope and, worse, would silently stop covering tools added later. It had
    already drifted: mail send/reply and the vault reads landed approval-gated this
    sprint and never reached the old constant.

    Three sources, one list:

    - **statically marked** — ``requires_approval=True`` on the tool itself, read off the
      assembled mapping the agent runs against, so a new one is covered the day it lands;
    - **conditionally gated** — ``gated_tools``: tools that raise ``ApprovalRequired``
      from inside the call (the recall gate, the foreign-document provenance gate) and
      therefore can't be discovered by inspection. Each feature manifest declares its
      own (``gated_tools``); the app assembles the union alongside the categories;
    - **external** — read from the operator's own sources.

    The external half reads the cached catalog (both services list from the database), so
    this never dials a server. A tool the operator has already marked *trusted* is still
    listed: trust is revocable, and a scope that vanished from the vocabulary the moment
    it stopped gating would fail validation the moment it was revoked.
    """
    static = {t.name for t in tool_catalog(categories)}
    scopes = [
        t
        for t in tool_catalog(categories)
        if _is_statically_gated(t, categories) or t.name in gated_tools
    ]
    # A conditionally-gated name that no longer matches a registered tool is a stale
    # declaration, not a scope — surfacing it would offer the operator a checkbox that
    # grants nothing. The set is small and pinned by a test, so this is a guard, not a
    # filter.
    scopes = [t for t in scopes if t.name in static]
    # A tool whose approval is never standing is not a scope either. It pauses a run,
    # but a grant on its *name* would not mean "this again" — for the egress request it
    # would mean every domain the agent goes on to name — so the engine drops such a
    # grant wherever one is written. Listed here, it would still be a checkbox on a
    # scheduled task's form that stores a scope the fire silently ignores.
    scopes = [t for t in scopes if t.name not in ONCE_ONLY_TOOLS]
    if external is not None:
        scopes.extend(await _external_scopes(external, owner_id))
    return sorted(scopes, key=lambda t: (t.category, t.name))


def _is_statically_gated(
    info: ToolInfo, categories: Mapping[str, AbstractToolset[RunDeps]]
) -> bool:
    """Whether the tool behind ``info`` carries ``requires_approval=True``."""
    toolset = categories.get(info.category)
    tool = getattr(toolset, "tools", {}).get(info.name.removeprefix(f"{info.category}_"))
    return bool(getattr(tool, "requires_approval", False))


async def _external_scopes(external: ExternalTools, owner_id: str) -> list[ToolInfo]:
    """The operator's external tools, named the way the model sees them.

    ``external_{slug}_{tool}`` mirrors how `tools/external.py` composes them — the
    category prefix, then the source's slug, then the tool's own name. Read from each
    service's stored catalog rather than by connecting, so listing the vocabulary costs
    a database read and never a process spawn or an HTTP handshake.
    """
    scopes: list[ToolInfo] = []
    for server in await external.mcp.list(owner_id):
        for tool in server.tools:
            scopes.append(
                ToolInfo(
                    name=f"external_{server.slug}_{tool.name}",
                    category="external",
                    description=_flatten(f"{server.name}: {tool.description}"),
                )
            )
    for connector in await external.integrations.list(owner_id):
        for action in connector.actions:
            scopes.append(
                ToolInfo(
                    name=f"external_{connector.slug}_{action.name}",
                    category="external",
                    description=_flatten(f"{connector.name}: {action.description}"),
                )
            )
    return scopes
