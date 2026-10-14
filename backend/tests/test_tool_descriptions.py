"""What a tool's description says, measured on the catalog a real run is handed.

Two of the three authors of that text are outside this repo — Pydantic AI, which wraps
any docstring with a ``Returns:`` section in XML, and ``pydantic_ai_harness``, whose
toolsets cross-reference each other by the un-namespaced names their own functions carry.
``tools/describe.py`` is the pass that fixes both, and the assertions worth having are
the ones taken over the **assembled** catalog rather than over hand-written samples: a
harness upgrade that reintroduces a leak, or a new category whose docstrings point at
names nothing is registered under, is exactly the regression this file exists to catch.
"""

from __future__ import annotations

import json
import re
from typing import cast

from pydantic import JsonValue
from pydantic_ai import RunContext, Tool, ToolDefinition
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage

from prompts.agent import INSTRUCTIONS
from runs import Run, RunStream
from services.workspace_kind import WORKSPACE_TOOLS
from tools import RunDeps, build_agent_toolsets
from tools.catalog import tool_catalog
from tools.describe import (
    _SUBTASKS_LEAKED,
    NARRATION_ARG,
    NARRATION_PROPERTY,
    NARRATION_TEXT,
    add_property,
    category_names,
    category_of,
    describe,
    describe_text,
    drop_properties,
    prefix_references,
    strip_docstring_xml,
    strip_schema_leaks,
)

from ._helpers import full_tool_categories
from ._json import arr, obj, parse_json, s

OWNER = "operator"

#: A single-line backticked span — the form every docstring here marks an identifier in.
_BACKTICKED = re.compile(r"(?P<ticks>`+)(?P<body>[^`\n]+?)(?P=ticks)")


async def _offered() -> dict[str, ToolDefinition]:
    """Every tool definition a run is actually handed in any workspace, through the
    composed stack the engine gives the Agent — not a re-derivation, so what is asserted
    below is what the model reads."""
    run = Run(id="t", kind="chat", owner_id=OWNER, stream=RunStream())
    offered: dict[str, ToolDefinition] = {}
    for kind in WORKSPACE_TOOLS:
        deps = RunDeps(run=run, owner_id=OWNER, disabled_tools=frozenset(), workspace_kind=kind)
        ctx = RunContext(deps=deps, model=TestModel(), usage=RunUsage())
        tools = await build_agent_toolsets(full_tool_categories())[0].get_tools(ctx)
        offered |= {name: tool.tool_def for name, tool in tools.items()}
    return offered


def _flatten(text: str) -> str:
    return " ".join(text.split())


def _schema(tool_def: ToolDefinition) -> dict[str, JsonValue]:
    return obj(parse_json(json.dumps(tool_def.parameters_json_schema)))


def _at(value: JsonValue, *path: str) -> JsonValue:
    for key in path:
        value = obj(value)[key]
    return value


def _raw_tools(toolset: object) -> dict[str, Tool[RunDeps]]:
    """The tools a toolset carries on a ``tools`` attribute, if it has one."""
    tools: object = getattr(toolset, "tools", None)
    if not isinstance(tools, dict):
        return {}
    return cast(dict[str, Tool[RunDeps]], tools)


# --- the assembled catalog ----------------------------------------------------------


async def test_no_offered_description_carries_the_librarys_docstring_xml():
    for name, tool_def in (await _offered()).items():
        assert "<summary>" not in (tool_def.description or ""), name
        assert "<returns>" not in (tool_def.description or ""), name


async def test_the_browser_does_not_offer_its_per_call_timeout():
    offered = await _offered()
    browse = [name for name in offered if name.startswith("browse_")]
    assert browse, "the browse category vanished; this test is no longer measuring it"
    for name in browse:
        assert "timeout_ms" not in json.dumps(offered[name].parameters_json_schema), name


async def test_no_description_points_at_a_name_that_is_not_offered():
    # A bare name is resolved within the referring tool's own category, which is the only
    # place it is unambiguous — so that is the claim tested. A cross-category reference
    # has to be written out in full by whoever wrote the docstring.
    offered = await _offered()
    names = category_names(full_tool_categories())
    for name, tool_def in offered.items():
        category = category_of(name, names)
        text = (tool_def.description or "") + json.dumps(tool_def.parameters_json_schema)
        for match in _BACKTICKED.finditer(text):
            head = match["body"].split("(")[0]
            assert head not in names[category or ""], (
                f"{name} points at `{head}`, which is offered as `{category}_{head}`"
            )


async def test_no_schema_leaks_the_harnesss_subtasks_mode():
    for name, tool_def in (await _offered()).items():
        schema = json.dumps(tool_def.parameters_json_schema)
        for leak in ("enable_subtasks", "subtasks mode", "subtasks to be enabled", "Attributes:"):
            assert leak not in schema, f"{name} still describes {leak!r}"


async def test_the_plan_schema_offers_only_what_the_store_accepts():
    write_plan = _schema((await _offered())["tasks_write"])
    item = obj(_at(write_plan, "$defs", "PlanItem", "properties"))
    assert "parent_id" not in item
    assert "depends_on" not in item
    statuses = arr(_at(write_plan, "$defs", "TaskStatus", "enum"))
    assert "blocked" not in statuses
    # The statuses the store does accept are all still there.
    assert set(statuses) == {
        "pending",
        "in_progress",
        "completed",
        "cancelled",
    }


async def test_the_explanation_rule_is_stated_once_in_the_standing_brief():
    """The brief already tells the model on every turn what an explanation argument is
    for. A tool description repeating it buys the same sentence a second time in the
    cached prefix; what belongs in the description is the half that is this tool's — who
    receives it, what the command does, what the credential is for."""
    assert "explanation or reason argument" in INSTRUCTIONS
    for name, tool_def in (await _offered()).items():
        assert "operator reads when deciding" not in (tool_def.description or ""), name


async def test_every_tool_is_offered_somewhere_to_say_why():
    """The work log shows a call's arguments, which say what is about to happen and never
    why. The narration is that missing half — and the collapsed log leads with the run's
    latest one — so the claim worth pinning is over the whole assembled catalog, reads
    included: a read-heavy turn would otherwise show no reason for most of its length."""
    offered = await _offered()
    assert offered, "the catalog is empty; this test is no longer measuring anything"
    for name in offered:
        schema = _schema(offered[name])
        properties = obj(schema["properties"])
        assert NARRATION_ARG in properties, f"{name} cannot say why"
        assert obj(properties[NARRATION_ARG])["description"] == NARRATION_TEXT, name
        # Optional by construction: a model that leaves it out gets a call that runs with
        # no narration, not a retry prompt about a property nothing declares.
        assert NARRATION_ARG not in arr(schema.get("required", [])), name


async def test_every_tool_named_as_leaking_is_one_the_catalog_offers():
    """The strip is keyed by name, so a task-list tool the allowlist stops registering
    leaves a dead entry behind — and a dead entry is a test asserting over a tool no model
    will ever see, which reads as coverage and is not. It is keyed by the name *we* give
    the tool, not the harness's, so the rename in ``tools/tasks.py`` has to have reached
    here too."""
    offered = await _offered()
    assert _SUBTASKS_LEAKED <= set(offered)


async def test_the_plan_schema_says_each_thing_once():
    """Every byte here ships on every request while the plan tools are loaded, and the
    def sits directly above the property it would be repeating."""
    item = obj(_at(_schema((await _offered())["tasks_write"]), "$defs", "PlanItem"))
    assert "auto" not in s(item["description"]).lower()
    assert "Auto-generated" in s(_at(item, "properties", "id", "description"))


async def test_the_operator_reads_the_description_the_model_reads():
    # The settings screen exists so the operator can decide whether to allow a tool. A
    # description that differs from the model's is a different decision.
    offered = await _offered()
    for info in tool_catalog(full_tool_categories()):
        assert info.description == _flatten(offered[info.name].description or ""), info.name


async def test_describing_preserves_everything_that_carries_policy():
    # The stage runs after the approval gate, so an `unapproved` kind rewritten there has
    # to survive it — a copy-editing pass that ungated a tool would be a security bug.
    offered = await _offered()
    raw = {
        f"{category}_{tool_name}": tool
        for category, toolset in full_tool_categories().items()
        for tool_name, tool in _raw_tools(toolset).items()
    }
    for name, tool_def in offered.items():
        assert tool_def.metadata == raw[name].metadata, name
        assert tool_def.defer_loading == raw[name].defer_loading, name
        assert tool_def.sequential == raw[name].sequential, name
        assert tool_def.strict == raw[name].strict, name


async def test_the_raw_toolsets_are_left_as_the_library_built_them():
    # Other readers (the sensitivity classifier, `test_code_tools.py`) hold these objects.
    categories = full_tool_categories()
    before = {
        name: (tool.description, json.dumps(tool.function_schema.json_schema, sort_keys=True))
        for toolset in categories.values()
        for name, tool in _raw_tools(toolset).items()
    }
    run = Run(id="t", kind="chat", owner_id=OWNER, stream=RunStream())
    deps = RunDeps(run=run, owner_id=OWNER, disabled_tools=frozenset())
    ctx = RunContext(deps=deps, model=TestModel(), usage=RunUsage())
    _ = await build_agent_toolsets(categories)[0].get_tools(ctx)
    _ = tool_catalog(categories)
    after = {
        name: (tool.description, json.dumps(tool.function_schema.json_schema, sort_keys=True))
        for toolset in categories.values()
        for name, tool in _raw_tools(toolset).items()
    }
    assert before == after


# --- the pure functions -------------------------------------------------------------

#: A harness docstring as Pydantic AI hands it over: a summary, then the returns block.
_XML_DOCSTRING = (
    "<summary>Use `snapshot`.</summary>\n<returns>\n<description>X.</description>\n</returns>"
)

NAMES = {
    "browse": frozenset({"snapshot", "click", "tabs", "press_key"}),
    "files": frozenset({"read_file", "find_files"}),
    "corpus": frozenset({"retrieve"}),
    "project": frozenset({"list"}),
}


def test_strip_docstring_xml_keeps_the_summary_and_drops_the_returns_block():
    assert (
        strip_docstring_xml(
            "<summary>Click an element.\n\nMore.</summary>\n"
            + "<returns>\n<description>The text after.</description>\n</returns>"
        )
        == "Click an element.\n\nMore."
    )


def test_strip_docstring_xml_keeps_a_typed_returns_only_docstring():
    # `main_desc` is the returns block alone when the docstring had no summary; dropping
    # it would leave the tool described by nothing at all.
    assert (
        strip_docstring_xml(
            "<returns>\n<type>str</type>\n<description>Matching lines.</description>\n</returns>"
        )
        == "Matching lines."
    )


def test_strip_docstring_xml_leaves_plain_prose_and_none_alone():
    assert strip_docstring_xml("Just a sentence with a `<select>` in it.") == (
        "Just a sentence with a `<select>` in it."
    )
    assert strip_docstring_xml(None) is None
    assert strip_docstring_xml("") == ""


def test_strip_docstring_xml_is_idempotent():
    once = strip_docstring_xml(_XML_DOCSTRING)
    assert strip_docstring_xml(once) == once


def test_drop_properties_takes_the_requirement_with_the_property():
    schema = {
        "type": "object",
        "properties": {"url": {"type": "string"}, "timeout_ms": {"type": "integer"}},
        "required": ["url", "timeout_ms"],
    }
    dropped = drop_properties(schema, {"timeout_ms"})
    assert dropped["properties"] == {"url": {"type": "string"}}
    assert dropped["required"] == ["url"]
    # The caller's schema is untouched — it belongs to a toolset other code reads.
    assert "timeout_ms" in schema["properties"]


def test_drop_properties_removes_an_emptied_required_list():
    dropped = drop_properties(
        {"type": "object", "properties": {"timeout_ms": {}}, "required": ["timeout_ms"]},
        {"timeout_ms"},
    )
    assert "required" not in dropped


def test_add_property_offers_the_property_without_requiring_it():
    schema = {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    }
    added = add_property(schema, NARRATION_ARG, NARRATION_PROPERTY)
    assert added["properties"][NARRATION_ARG]["description"] == NARRATION_TEXT
    assert added["required"] == ["path"]
    # The caller's schema is untouched — it belongs to a toolset other code reads.
    assert NARRATION_ARG not in schema["properties"]


def test_add_property_gives_a_no_argument_tool_somewhere_to_put_it():
    # The shape Pydantic AI hands over for a tool that takes nothing, and the case where a
    # narration is worth most: the call's arguments say nothing at all by themselves.
    added = add_property({"type": "object"}, NARRATION_ARG, NARRATION_PROPERTY)
    assert added["properties"] == {NARRATION_ARG: dict(NARRATION_PROPERTY)}


def test_add_property_is_idempotent():
    once = add_property({"type": "object", "properties": {}}, NARRATION_ARG, NARRATION_PROPERTY)
    assert add_property(once, NARRATION_ARG, NARRATION_PROPERTY) == once


def test_prefix_references_rewrites_backticked_and_compound_names():
    assert (
        prefix_references("pass one back to `click` or `press_key('Enter')`", NAMES, "browse")
        == "pass one back to `browse_click` or `browse_press_key('Enter')`"
    )
    # Unbackticked, but compound: a snake_case identifier cannot be a word of English.
    assert (
        prefix_references("Prefer this over read_file.", NAMES, "files")
        == "Prefer this over files_read_file."
    )


def test_prefix_references_leaves_a_word_that_is_not_this_categorys_tool():
    # `list` and `select` are a tab action here and a registered tool elsewhere; resolving
    # bare names across categories would rewrite them into a tool that does something else.
    text = "a flow needs `select` first, so a `list` that does not show one is worth repeating"
    assert prefix_references(text, NAMES, "browse") == text
    # A single bare word is left alone even in its own category unless it is backticked.
    assert prefix_references("Open a new tabs page", NAMES, "browse") == "Open a new tabs page"


def test_prefix_references_rewrites_a_dotted_name_from_any_category():
    assert (
        prefix_references("prefer ``corpus.retrieve`` with the id", NAMES, "attachments")
        == "prefer ``corpus_retrieve`` with the id"
    )


def test_prefix_references_is_idempotent():
    for text, category in (
        ("pass one back to `click`", "browse"),
        ("Prefer this over read_file.", "files"),
        ("prefer ``corpus.retrieve``", "attachments"),
    ):
        once = prefix_references(text, NAMES, category)
        assert prefix_references(once, NAMES, category) == once


def test_prefix_references_without_a_category_still_resolves_dotted_names():
    assert prefix_references("`click` and `corpus.retrieve`", NAMES, None) == (
        "`click` and `corpus_retrieve`"
    )


def test_category_of_reads_the_namespaced_name():
    assert category_of("browse_press_key", NAMES) == "browse"
    assert category_of("browse_nothing", NAMES) is None


def test_strip_schema_leaks_only_touches_the_tools_it_names():
    schema = {"$defs": {"TaskStatus": {"enum": ["pending", "blocked"], "title": "TaskStatus"}}}
    assert strip_schema_leaks("tasks_update_statuses", schema)["$defs"]["TaskStatus"] == {
        "enum": ["pending"],
        "description": "Lifecycle status of a task.",
    }
    assert strip_schema_leaks("browse_click", schema) == schema
    # The caller's schema is copied, never edited.
    assert schema["$defs"]["TaskStatus"]["enum"] == ["pending", "blocked"]


def test_describe_is_idempotent():
    tool_def = ToolDefinition(
        name="browse_click",
        description=_XML_DOCSTRING,
        parameters_json_schema={
            "type": "object",
            "properties": {"selector": {"type": "string", "description": "From `snapshot`."}},
        },
    )
    once = describe(tool_def, NAMES)
    assert once.description == "Use `browse_snapshot`."
    assert describe(once, NAMES) == once


def test_describe_text_matches_what_describe_puts_on_the_definition():
    assert describe_text("browse_click", _XML_DOCSTRING, NAMES) == (
        describe(ToolDefinition(name="browse_click", description=_XML_DOCSTRING), NAMES).description
    )
