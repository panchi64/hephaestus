"""The toolset access-policy stack over Pydantic AI primitives.

Each category toolset is namespaced to ``category_tool`` names, combined, then passed
through the **enabled gate** so an operator-disabled tool is never offered. There is no
relevance pre-filter: a category a manifest declares dormant is offered with its schemas
withheld until the model asks for the group by name (``tool_search.py``).

This module owns only the **core** categories (builtin tools and the sandbox code runner,
wired by ``app.py``). Every feature category arrives from its manifest's ``toolsets``
export and is folded in at app assembly.

Sensitive-action gating is not a filter: those tools pause for operator approval at
execution time. The **approval gate** marks every tool that reaches past the permission
level's write scope as needing approval, so the call returns to the engine undone. A
tool's own marking is left alone; the gate only adds (``services/permissions``).

Last is the **describing stage** (``describe.py``), which owns no policy: it rewrites each
definition's model-facing text into the namespaced names and drops library scaffolding.
It runs after the gates so a description cannot decide whether a tool is offered. It also
offers every tool a ``narration`` argument (the model's reason for a call, shown in the
work log), which ``tools/narration.py`` strips before validation.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import replace

from pydantic_ai import AbstractToolset, CombinedToolset, RunContext, ToolDefinition

from services.permissions import beyond_scope
from services.permissions.guards import COMMAND_TOOLS
from services.tool_policy import permission_disabled_tools
from services.tool_sensitivity import declared_sensitivity

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
from .guarded import GuardedToolset
from .harness_events import own_harness_events
from .plan import GATED_TOOLS as _PLAN_GATED
from .plan import plan_toolset
from .recover import RecoveringToolset, TolerantTools
from .report import REPORT_TOOL
from .shell import GATED_TOOLS as _SHELL_GATED
from .shell import shell_toolset
from .tasks import tasks_toolset

#: Names of core tools that raise `ApprovalRequired` from inside the call, which inspection
#: cannot discover. Core categories have no manifest, so they are seeded into
#: `app.state.gated_tools` at assembly; a name missing there cannot be granted.
CORE_GATED_TOOLS: frozenset[str] = _SHELL_GATED | _PLAN_GATED | _FILES_GATED


def _enabled_gate(ctx: RunContext[RunDeps], tool_def: ToolDefinition) -> bool:
    """Withheld tools are not offered to or invoked by the agent.

    ``disabled_tools`` is settled for the whole turn (``services/tool_policy.py``) and
    ``tool_access`` is what the run's agent is offered at all. The level can move inside a
    turn (``tools/plan.py``), so it is read live rather than folded into that union.
    """
    name = tool_def.name
    deps = ctx.deps
    if name == REPORT_TOOL:
        # No setting or level may take away how a spawned agent finishes.
        return deps.reports_to_parent
    return (
        name not in deps.disabled_tools
        and deps.tool_access.permits(name)
        and name not in permission_disabled_tools(deps.permission)
    )


def _approval_gate(
    ctx: RunContext[RunDeps], tool_defs: list[ToolDefinition]
) -> list[ToolDefinition]:
    """Mark every tool that reaches past this run's permission level as needing approval.

    Pydantic AI defers a call to an ``unapproved`` tool, as it does for
    ``requires_approval=True``, so the engine rules on the call and its arguments before
    anything happens (``services/permissions/decide.py``).

    The definition is rewritten, not filtered out: a withheld tool says the capability does
    not exist, a gated one says it needs permission, which is true at every level except
    Plan, and Plan withholds in the catalog to save schema tokens.

    Only ``function`` tools are touched. An ``output`` tool ends the run and an ``external``
    one is already deferred; marking either would change what the deferral means.
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
    """Offer this tool a ``narration`` argument, reads included: a name and arguments say
    what a call does, never why, and the work log leads with the latest reason. The
    property is charged on every request (``tests/test_tool_schema_cost.py``).

    Unconditional, so there is no second sensitivity list to rot beside the catalog.

    **Except a tool that already declares its own ``narration``** (an MCP server's or an
    integration's). Overwriting it would give the model our description for their argument
    and the strip would take the value from a tool that needed it; ``tools/narration.py``
    recognises ours by its exact subschema. The property comes off before validation.
    """
    if NARRATION_ARG in (tool_def.parameters_json_schema.get("properties") or {}):
        return tool_def
    return replace(
        tool_def,
        parameters_json_schema=add_property(
            tool_def.parameters_json_schema, NARRATION_ARG, NARRATION_PROPERTY
        ),
    )


def _alone(_ctx: RunContext[RunDeps], tool_defs: list[ToolDefinition]) -> list[ToolDefinition]:
    """Run each tool that runs a command alone: calls before it in the batch finish first,
    it runs by itself, and calls after it start once it is done.

    The guard judges a relative path against the directory the shell stands in, which a
    `cd` would otherwise race. This orders calls in a batch only; these tools are never
    folded into `run_code`, because they carry `code_arg_name`.
    """
    return [
        replace(tool_def, sequential=True) if tool_def.name in COMMAND_TOOLS else tool_def
        for tool_def in tool_defs
    ]


def core_categories() -> dict[str, AbstractToolset[RunDeps]]:
    """The categories the harness itself contributes; everything else is a manifest's."""
    return {
        "builtin": builtin_toolset(),
        "code": code_toolset(),
        "files": files_toolset(),
        # `tasks` is the checklist kept at every level, `plan` the document a Plan-level
        # turn submits for approval.
        "tasks": tasks_toolset(),
        "plan": plan_toolset(),
        "shell": shell_toolset(),
    }


def build_agent_toolsets(
    categories: Mapping[str, AbstractToolset[RunDeps]] | None = None,
    dormant: Collection[str] = (),
    *,
    call_timeout_s: float | None = None,
) -> list[AbstractToolset[RunDeps]]:
    """Compose the gated, namespaced toolset stack handed to the Agent.

    ``categories`` is the assembled mapping (core + every manifest's export); ``None`` (a
    stateless/test turn) composes the core categories only. ``dormant`` names categories
    whose schemas are withheld until revealed (``tool_search.py``); an unknown name is
    ignored, since a category can be absent for reasons this layer cannot see.

    **Deferral is marked innermost, before every gate.** The enabled filter runs outside
    it, so a switched-off tool never enters the search corpus and cannot be revealed by
    naming its group; the approval gate and describing pass also run outside it, so a
    revealed tool carries the same ``unapproved`` kind and description it would have had
    undeferred. ``call_timeout_s`` bounds each call (``recover.py``).
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
        """Restate each tool in this catalog's names, offer it a place to say why, and stamp
        the owner its events are attributed to.

        The stamp goes on every tool: a per-category list would rot, and a lifted harness
        toolset whose events nobody owns fails mid-turn on its first mutation. It is safe
        broadly (both `capability_id` gates require `defer_loading`, which this owner never
        declares) and yields to a definition that names an owner (`tools/harness_events.py`).

        Narration is added after the rewrite: its text is already written against the
        namespaced catalog.
        """
        return [own_harness_events(_narrated(describe(tool_def, names))) for tool_def in tool_defs]

    # Namespaced, filtered, gated, then described: the gate classifies by the prefixed name,
    # and the rewrite resolves cross-references against those names (`describe.py`).
    # Recovery wraps all of it, so a call that raises is reported to the model from any
    # layer; the guard sits just inside it.
    stack = combined.filtered(_enabled_gate).prepared(_approval_gate).prepared(_describe)
    stack = stack.prepared(_alone)
    return [RecoveringToolset(GuardedToolset(stack), call_timeout_s=call_timeout_s)]
