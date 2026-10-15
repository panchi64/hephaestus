"""Dormant categories — the groups the model loads for itself, mid-turn.

The saving only counts if all three halves hold at once: the schemas really are off the
first request, naming a group really brings **all** of it back, and nothing the chassis
withheld for its own reasons — an operator's switch, a permission level — comes back with
it. So these tests drive a real ``Agent`` over a ``FunctionModel`` and read the tool list
the model was actually handed on each step, rather than asserting about the toolset stack
in isolation: deferral is resolved by the *model request*, and a stack that marks a tool
correctly can still ship it.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import cast

from fastapi import FastAPI
from pydantic_ai import Agent, AgentRunResult, DeferredToolRequests, ModelRequest, RunContext
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.toolsets import AbstractToolset
from pydantic_ai.usage import RunUsage

from agent.factory import build_agent
from harness.manifest import DormantCategory
from routes.deps import app_state
from runs import Run, RunStream
from tools import RunDeps, build_agent_toolsets
from tools.catalog import tool_catalog
from tools.describe import category_names
from tools.tool_search import dormant_index_instructions, tool_search_capability

from ._helpers import client_app, full_tool_categories
from ._json import json_list, obj, parse_json, s

OWNER = "operator"

#: What the manifests declare dormant, restated here so a category that quietly joins or
#: leaves the set is a failing test rather than a silent change in what every request
#: costs. Pinned against the booted app below, and only ever a *subset* claim about the
#: catalog — this file has no business asserting the whole of it.
DORMANT = {
    "agents": "spawn agents to do self-contained work",
    "browse": "drive a real browser",
    "calendar": "the operator's calendars",
    "mail": "the operator's email",
    "plugins": "the operator's published plugins",
    "routines": "schedule work to run later or on repeat",
    "vault": "the operator's stored secrets",
}


def _deps(disabled: frozenset[str] | None = None) -> RunDeps:
    run = Run(id="t", kind="chat", owner_id=OWNER, stream=RunStream())
    return RunDeps(run=run, owner_id=OWNER, disabled_tools=disabled or frozenset())


def _dormant(app: FastAPI) -> tuple[DormantCategory, ...]:
    return app_state(app).dormant_categories


def _registered(app: FastAPI) -> dict[str, AbstractToolset[RunDeps]]:
    return app_state(app).tool_categories


def _tool_names_of(toolset: object) -> frozenset[str]:
    tools: object = getattr(toolset, "tools", None)
    if not isinstance(tools, dict):
        return frozenset()
    return frozenset(cast(dict[str, object], tools))


def _agent(
    categories: Mapping[str, AbstractToolset[RunDeps]], *, model: FunctionModel
) -> Agent[RunDeps, str]:
    """The stack a real turn composes, minus everything that has nothing to do with
    deferral — the same toolsets and the same capability the engine is wired with."""
    return Agent(
        model,
        deps_type=RunDeps,
        toolsets=build_agent_toolsets(categories, dormant=tuple(DORMANT)),
        capabilities=[tool_search_capability(category_names(categories), DORMANT)],
    )


def _text_model(steps: list[set[str]]) -> FunctionModel:
    """A model that answers on the first step, so the run records the tool list and the
    brief it was handed and nothing else."""

    def respond(_messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        steps.append({t.name for t in info.function_tools})
        return ModelResponse(parts=[TextPart("done")])

    return FunctionModel(respond)


def _reveal_browse(steps: list[set[str]]) -> FunctionModel:
    """A model that asks for the browser on its first step and then stops."""

    def respond(_messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        steps.append({t.name for t in info.function_tools})
        if len(steps) == 1:
            return ModelResponse(parts=[ToolCallPart("search_tools", {"queries": ["browse"]})])
        return ModelResponse(parts=[TextPart("done")])

    return FunctionModel(respond)


# --- the declaration itself ----------------------------------------------------------


async def test_the_declared_dormant_set_is_what_the_app_assembles():
    async with client_app() as (_, app):
        assert {entry.category for entry in _dormant(app)} == set(DORMANT)
        # Every one of them is a registered category, so the index can never advertise a
        # group whose reveal comes back empty.
        assert set(DORMANT) <= set(_registered(app))


async def test_dormant_tools_are_deferred_and_the_rest_are_not():
    ctx = RunContext(deps=_deps(), model=TestModel(), usage=RunUsage())
    stack = build_agent_toolsets(full_tool_categories(), dormant=tuple(DORMANT))[0]
    tools = await stack.get_tools(ctx)
    assert tools["browse_click"].tool_def.defer_loading is True
    assert tools["mail_read"].tool_def.defer_loading is True
    assert tools["builtin_now"].tool_def.defer_loading is False
    assert tools["files_read_file"].tool_def.defer_loading is False


# --- what the first request actually costs -------------------------------------------


async def test_a_fresh_request_carries_no_dormant_schema():
    steps: list[set[str]] = []
    _ = await _agent(full_tool_categories(), model=_reveal_browse(steps)).run("hi", deps=_deps())
    first = steps[0]
    assert not [name for name in first if name.startswith("browse_")]
    assert not [name for name in first if name.startswith("mail_")]
    # The tools that are not dormant are untouched, and the way back in is offered.
    assert "builtin_now" in first
    assert "search_tools" in first


async def test_naming_a_group_reveals_the_whole_group_on_the_next_step():
    categories = full_tool_categories()
    steps: list[set[str]] = []
    _ = await _agent(categories, model=_reveal_browse(steps)).run("hi", deps=_deps())
    revealed = {name for name in steps[1] if name.startswith("browse_")}
    assert "browse_click" in revealed
    assert revealed == {f"browse_{name}" for name in category_names(categories)["browse"]}
    # A reveal is one group, not the end of deferral: the others stay held back.
    assert not [name for name in steps[1] if name.startswith("calendar_")]


async def test_a_revealed_tool_still_arrives_needing_approval():
    """Deferral is orthogonal to the permission level, and has to stay that way — a tool
    that shed its approval gate by being loaded late would be a hole with a schedule."""
    categories = full_tool_categories()
    steps: list[set[str]] = []
    kinds: dict[str, str] = {}

    def respond(_messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        steps.append({t.name for t in info.function_tools})
        kinds.update({t.name: t.kind for t in info.function_tools})
        if len(steps) == 1:
            return ModelResponse(parts=[ToolCallPart("search_tools", {"queries": ["mail"]})])
        return ModelResponse(parts=[TextPart("done")])

    _ = await _agent(categories, model=FunctionModel(respond)).run("hi", deps=_deps())
    assert kinds["mail_send"] == "unapproved"
    assert kinds["mail_list_messages"] == "function"


async def test_a_disabled_tool_never_enters_the_search_corpus():
    """The enabled gate runs outside the deferral marking, so an operator's switch is not
    something a search can talk its way past."""
    categories = full_tool_categories()
    steps: list[set[str]] = []
    _ = await _agent(categories, model=_reveal_browse(steps)).run(
        "hi", deps=_deps(frozenset({"browse_click"}))
    )
    assert "browse_click" not in steps[1]
    assert "browse_navigate" in steps[1]


async def test_a_description_matching_the_work_finds_the_group_without_its_name():
    """The group name is the reliable path in; keyword matching is the fallback for a
    model that never learned the name."""
    categories = full_tool_categories()
    steps: list[set[str]] = []

    def respond(_messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        steps.append({t.name for t in info.function_tools})
        if len(steps) == 1:
            return ModelResponse(
                parts=[ToolCallPart("search_tools", {"queries": ["read the inbox"]})]
            )
        return ModelResponse(parts=[TextPart("done")])

    _ = await _agent(categories, model=FunctionModel(respond)).run("hi", deps=_deps())
    assert [name for name in steps[1] if name.startswith("mail_")]


# --- the index that makes any of this reachable --------------------------------------


async def _index(disabled: frozenset[str] | None = None) -> str:
    provider = dormant_index_instructions(DORMANT, category_names(full_tool_categories()))
    ctx = RunContext(deps=_deps(disabled), model=TestModel(), usage=RunUsage())
    return await provider(ctx)


async def test_the_index_names_every_live_group_and_the_way_in():
    text = await _index()
    for category, summary in DORMANT.items():
        assert f"- {category}: {summary}" in text
    assert "search_tools" in text


async def test_the_index_drops_a_group_the_operator_switched_off_entirely():
    categories = full_tool_categories()
    all_vault = frozenset(f"vault_{name}" for name in category_names(categories)["vault"])
    text = await _index(all_vault)
    assert "- vault:" not in text
    assert "- browse:" in text
    # One tool of a group still leaves the group worth naming — the rest of it works.
    partial = await _index(frozenset({next(iter(all_vault))}))
    assert "- vault:" in partial


async def test_the_index_is_empty_when_every_group_is_off():
    categories = full_tool_categories()
    everything = frozenset(
        f"{category}_{name}"
        for category in DORMANT
        for name in _tool_names_of(categories[category])
    )
    assert await _index(everything) == ""


async def test_the_index_is_byte_stable_across_a_conversation():
    """It renders at the head of every request, so a byte that churns costs the local
    engine's prompt-prefix cache the whole history behind it."""
    assert await _index() == await _index()


# --- the operator's catalog says so too ----------------------------------------------


async def test_the_catalog_marks_the_dormant_rows():
    catalog = tool_catalog(full_tool_categories(), frozenset(DORMANT))
    rows = {t.name: t.dormant for t in catalog}
    assert rows["browse_click"] is True
    assert rows["builtin_now"] is False


async def test_the_tools_route_reports_dormancy():
    async with client_app() as (client, _):
        rows = {s(obj(row)["name"]): obj(row) for row in json_list(await client.get("/tools"))}
        assert rows["browse_click"]["dormant"] is True
        # Dormant is not disabled: the operator's own switch is still on.
        assert rows["browse_click"]["enabled"] is True
        assert rows["builtin_now"]["dormant"] is False


# --- the engine composes the real thing this way -------------------------------------


def _brief(result: AgentRunResult[DeferredToolRequests | str]) -> str:
    """The instructions every request in a finished run shipped with."""
    return "\n".join(
        m.instructions or "" for m in result.all_messages() if isinstance(m, ModelRequest)
    )


async def test_the_engine_defers_and_reveals_exactly_as_this_stack_does():
    """Everything above composes the two halves by hand. This is the one assertion that
    the *engine* wires them — a stack that defers correctly and an agent built without the
    capability would leave the model looking at a hole where the browser used to be."""
    categories = full_tool_categories()
    steps: list[set[str]] = []
    _ = await build_agent(_reveal_browse(steps), categories=categories, dormant=DORMANT).run(
        "hi", deps=_deps()
    )
    assert not [name for name in steps[0] if name.startswith("browse_")]
    assert "search_tools" in steps[0]
    revealed = {name for name in steps[1] if name.startswith("browse_")}
    assert "browse_click" in revealed
    assert revealed == {f"browse_{name}" for name in category_names(categories)["browse"]}


def _defs_model(defs: list[list[ToolDefinition]]) -> FunctionModel:
    """A model that answers immediately and keeps the tool *definitions* it was handed,
    for the assertions that are about schema text rather than about which names arrived."""

    def respond(_messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        defs.append(list(info.function_tools))
        return ModelResponse(parts=[TextPart("done")])

    return FunctionModel(respond)


async def _search_tools_def(model: FunctionModel | None = None) -> ToolDefinition:
    defs: list[list[ToolDefinition]] = []
    _ = await _agent(full_tool_categories(), model=model or _defs_model(defs)).run(
        "hi", deps=_deps()
    )
    return next(d for d in defs[0] if d.name == "search_tools")


async def test_the_queries_parameter_describes_what_actually_resolves():
    """The library's default text tells the model to use words likely to appear in tool
    names and descriptions — the one thing a dormant group has none of, since withholding
    them is the whole point. It steers the model onto the keyword fallback and away from
    the group name the strategy resolves on."""
    search = await _search_tools_def()
    schema = obj(parse_json(json.dumps(search.parameters_json_schema)))
    queries = obj(obj(schema["properties"])["queries"])
    assert "tool names" not in s(queries["description"])
    assert "group's name" in s(queries["description"])


async def test_the_search_tools_description_does_not_restate_the_index():
    """Both ride in the cached prefix of every request while anything is dormant, so a
    rule stated in both is paid for twice. The roster, the fact that a loaded group stays
    loaded, and the rule about loading before refusing live in the index."""
    description = (await _search_tools_def()).description or ""
    for restated in (*DORMANT, "rest of the conversation", "cannot be done"):
        assert restated not in description


async def test_the_engine_puts_the_index_in_the_standing_brief():
    steps: list[set[str]] = []
    result = await build_agent(
        _text_model(steps), categories=full_tool_categories(), dormant=DORMANT
    ).run("hi", deps=_deps())
    brief = _brief(result)
    for category, summary in DORMANT.items():
        assert f"- {category}: {summary}" in brief


async def test_the_index_is_still_true_once_a_group_has_been_loaded():
    """The block is byte-stable so the prompt-prefix cache survives the conversation,
    which means it is re-sent unchanged after a reveal — and stays re-sent, since a
    compaction fold carries the reveals across. A line asserting the browser is not
    loaded, shipped alongside every browser tool, gets acted on: the model searches for
    what it already holds, or tells the operator it cannot open a page."""
    steps: list[set[str]] = []
    result = await build_agent(
        _reveal_browse(steps), categories=full_tool_categories(), dormant=DORMANT
    ).run("hi", deps=_deps())
    assert [name for name in steps[1] if name.startswith("browse_")]
    brief = _brief(result)
    assert "- browse: drive a real browser" in brief
    for false_once_loaded in ("not loaded", "not in your list", "held back"):
        assert false_once_loaded not in brief


async def test_an_installation_with_nothing_dormant_pays_for_none_of_it():
    """No deferral, no index, and no `search_tools` on the wire — the seam costs a turn
    that has nothing to hide exactly nothing."""
    steps: list[set[str]] = []
    result = await build_agent(
        _text_model(steps), categories=full_tool_categories(), dormant={}
    ).run("hi", deps=_deps())
    assert "browse_click" in steps[0]
    assert "search_tools" not in steps[0]
    assert "can be loaded on demand" not in _brief(result)
