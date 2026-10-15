"""The toolset access-policy stack — *our* policy over Pydantic AI primitives.

Which tools a run sees is composition, not bespoke machinery (the most leveraged
mapping in the design). Each category toolset is namespaced for stable
``category_tool`` names, combined, then passed through the **enabled gate** so an
operator-disabled tool is never offered. With a single operator (no privilege tiers)
there is no privilege gate.

Nor is there a **relevance pre-filter**: nothing here guesses at what this turn will
want. What some categories do carry is **deferral** — a category a manifest declares
dormant is offered with its schemas withheld until the model asks for the group by
name (``tool_search.py``), which is the same posture stated the other way round. The
chassis withholds and says so; the model, not a heuristic, decides what to load.

The category registry is not a central list: this module owns only the **core**
categories (the builtin starter tools and the sandbox code runner, both wired by
``app.py`` itself). Every feature category arrives from its own manifest's
``toolsets`` export and is folded in at app assembly — the assembled mapping is
what production passes down as ``categories``.

Sensitive-action gating is *not* a filter here — those tools pause for operator
approval at execution time, handled by the engine, not dropped from the catalog. What
*is* here is the **approval gate**: the run's permission level marks every tool that
reaches past its write scope as needing approval, so the model's request for one comes
back to the engine undone instead of running. A tool's own marking is left alone — the
gate only ever adds (``services/permissions``).

Last comes the **describing stage** (``describe.py``), which owns no policy at all: it
rewrites each definition's model-facing text into the namespaced names this stack has
just produced, and drops the library scaffolding and unreachable schema a
harness-supplied tool arrives with. It runs after the gates so a description can never
decide whether a tool is offered. It is also where every tool is offered its
``narration`` argument — one sentence of the model's own reason for a call, for
the work log to show beside the arguments — which ``tools/narration.py`` takes back off
before anything validates it.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import replace

from pydantic_ai import AbstractToolset, CombinedToolset, RunContext, ToolDefinition

from services.permissions import beyond_scope
from services.tool_policy import permission_disabled_tools
from services.tool_sensitivity import declared_sensitivity
from services.workspace_kind import workspace_disabled_tools

from .builtin import builtin_toolset
from .code import code_toolset
from .deps import RunDeps
from .describe import (
    NARRATION_ARG,
    NARRATION_PROPERTY,
    add_property,
    category_names,
    describe,
)
from .files import GATED_TOOLS as _FILES_GATED
from .files import files_toolset
from .harness_events import own_harness_events
from .plan import GATED_TOOLS as _PLAN_GATED
from .plan import plan_toolset
from .recover import RecoveringToolset, TolerantTools
from .repo import repo_toolset
from .shell import GATED_TOOLS as _SHELL_GATED
from .shell import shell_toolset
from .tasks import tasks_toolset

#: Conditionally-gated names the **core** categories contribute — the ones that raise
#: `ApprovalRequired` from inside the call rather than carrying `requires_approval=True`,
#: which no amount of inspection can discover. A feature category declares its own on its
#: manifest; the core ones have no manifest, so they are collected here and seeded into
#: `app.state.gated_tools` at assembly. A name missing from that union is missing from the
#: operator's approval-scope vocabulary, which is what makes a grant possible.
CORE_GATED_TOOLS: frozenset[str] = _SHELL_GATED | _PLAN_GATED | _FILES_GATED


def _enabled_gate(ctx: RunContext[RunDeps], tool_def: ToolDefinition) -> bool:
    """Withheld tools are not offered to or invoked by the agent.

    ``disabled_tools`` is the union of everything settled for the whole turn
    (``services/tool_policy.py``), and ``tool_access`` is what the run's agent is offered at
    all. The **level** and the **workspace kind** can both move
    inside a turn (``tools/plan.py`` narrows and widens the level; ``project_open_worktree``
    moves the workspace), so they are read live on every request rather than folded into
    that union, where they could be added and never correctly taken back out.
    """
    name = tool_def.name
    deps = ctx.deps
    return (
        name not in deps.disabled_tools
        and deps.tool_access.permits(name)
        and name not in permission_disabled_tools(deps.permission)
        and name not in workspace_disabled_tools(deps.workspace_kind)
    )


def _approval_gate(
    ctx: RunContext[RunDeps], tool_defs: list[ToolDefinition]
) -> list[ToolDefinition]:
    """Mark every tool that reaches past this run's permission level as needing approval.

    The level's half of the enforcement. Pydantic AI defers a call to an ``unapproved``
    tool rather than executing it — the same mechanism a tool's own
    ``requires_approval=True`` uses — so the engine gets the call, with its arguments,
    before anything happens, and rules on it (``services/permissions/decide.py``).

    **Rewriting the definition rather than filtering it out** is what separates this from
    the enabled gate above. A withheld tool tells the model the capability does not exist;
    a gated one tells it the capability needs permission, which is the true statement at
    every level except Plan — and Plan does its withholding in the catalog, where the
    saving in schema tokens is real.

    Only ``function`` tools are touched. An ``output`` tool ends the run and an
    ``external`` one is already deferred to someone else; making either "unapproved" would
    change what the deferral *means* rather than merely when it happens.
    """
    return [
        replace(tool_def, kind="unapproved")
        if tool_def.kind == "function"
        and beyond_scope(
            ctx.deps.permission,
            tool_def.name,
            declared=declared_sensitivity(tool_def.metadata),
        )
        else tool_def
        for tool_def in tool_defs
    ]


def _narrated(tool_def: ToolDefinition) -> ToolDefinition:
    """Offer this tool a ``narration`` argument — every tool, reads included.

    Reads used to be left silent, on the theory that a read's name and arguments already
    say what it is doing. They say *what*, never *why*, and the work log now leads with the
    run's most recent reason: a turn that spends ten calls reading would show no reason at
    all for most of its length. The property is charged on every request whether or not the
    model writes one (``tests/test_tool_schema_cost.py`` records the price).

    Unconditional on purpose: a gate here keyed on sensitivity would be a second list of
    which tools explain themselves, rotting separately from the catalog.

    **Except a tool that already declares a ``narration`` of its own** — an MCP server's or
    an integration's, which this repo does not write. Overwriting it would hand the model
    our description for their argument, and the strip would then take the value away from
    a tool that needed it. Such a tool keeps its own; ``tools/narration.py`` recognises ours
    by its exact subschema and leaves every other one alone.

    The property comes off again before validation (``tools/narration.py``); a tool function
    never sees it.
    """
    if NARRATION_ARG in (tool_def.parameters_json_schema.get("properties") or {}):
        return tool_def
    return replace(
        tool_def,
        parameters_json_schema=add_property(
            tool_def.parameters_json_schema, NARRATION_ARG, NARRATION_PROPERTY
        ),
    )


def core_categories() -> dict[str, AbstractToolset[RunDeps]]:
    """The categories the harness itself contributes — everything else is a manifest's."""
    return {
        "builtin": builtin_toolset(),
        "code": code_toolset(),
        "files": files_toolset(),
        # The two halves of the split: `tasks` is the running checklist the agent keeps at
        # every level, `plan` the written document a Plan-level turn submits for approval.
        "tasks": tasks_toolset(),
        "plan": plan_toolset(),
        # The worktree's two categories. Registered unconditionally like every other and
        # withheld live from sandbox runs (`services/workspace_kind.py`) — one catalog, so
        # the operator's settings list and the agent's real stack cannot diverge.
        "shell": shell_toolset(),
        "repo": repo_toolset(),
    }


def build_agent_toolsets(
    categories: Mapping[str, AbstractToolset[RunDeps]] | None = None,
    dormant: Collection[str] = (),
    *,
    call_timeout_s: float | None = None,
) -> list[AbstractToolset[RunDeps]]:
    """Compose the gated, namespaced toolset stack handed to the Agent.

    ``categories`` is the assembled mapping (core + every manifest's export);
    ``None`` — a stateless/test turn that passed nothing — composes the core
    categories only. ``dormant`` names the categories whose schemas are withheld until
    the model reveals them (``tool_search.py``); an unknown name is ignored rather than
    fatal, since a category can be absent for reasons this layer cannot see.

    **Deferral is marked innermost, before every gate**, and the order is the whole
    point. The enabled filter runs outside it, so a tool the operator switched off never
    enters the search corpus and cannot be revealed by naming its group; the approval
    gate and the describing pass run outside it too, so a tool that arrives on reveal
    carries the same ``unapproved`` kind and the same rewritten description it would
    have carried had it never been dormant. ``call_timeout_s`` bounds each call
    (``recover.py``).
    """
    cats = dict(categories) if categories is not None else core_categories()
    deferred = frozenset(dormant)
    prefixed = [
        TolerantTools(toolset).prefixed(name).defer_loading()
        if name in deferred
        else TolerantTools(toolset).prefixed(name)
        for name, toolset in cats.items()
    ]
    combined = CombinedToolset(prefixed)
    names = category_names(cats)

    def _describe(
        _ctx: RunContext[RunDeps], tool_defs: list[ToolDefinition]
    ) -> list[ToolDefinition]:
        """Restate each tool in the names and vocabulary this catalog actually offers, offer
        each one a place to say why, and stamp the owner its events are attributed to.

        The stamp goes on every tool rather than only the harness-derived ones, because the
        alternative is a per-category list that silently rots: a lifted harness toolset whose
        events nobody owns does not fail at assembly, it fails mid-turn on the first mutation.
        It is safe to apply broadly — both places `capability_id` gates a tool require its
        owning capability to declare `defer_loading`, which this owner never does — and it
        yields to a definition that already names an owner (`tools/harness_events.py`).

        The narration argument is added last of the three, after the rewrite rather than
        before it: its text is ours, written against the namespaced catalog already, and
        sending it through a pass that resolves cross-references would be asking a rewrite
        to check a string this repo controls.
        """
        return [own_harness_events(_narrated(describe(tool_def, names))) for tool_def in tool_defs]

    # Namespaced, then filtered, then gated, then described — in that order because each
    # step needs the one before it: the gate classifies by the `category_tool` name the
    # prefixing produces, there is no point gating a tool the filter already took away,
    # and the rewrite resolves a description's cross-references against those same
    # namespaced names (`describe.py`). Recovery wraps all of it, so a call that raises is
    # reported to the model whatever layer it raised in.
    stack = combined.filtered(_enabled_gate).prepared(_approval_gate).prepared(_describe)
    return [RecoveringToolset(stack, call_timeout_s=call_timeout_s)]
