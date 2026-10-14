"""What the context window is holding, split three ways.

The provider reports one number and no breakdown, so the split is measured on our side —
the same posture as the wall-clock timings, and for the same reason: it has to mean the
same thing on Anthropic, on an OpenAI-compatible endpoint and on a local server.

The two properties that matter are asserted throughout. The parts always sum to exactly
the total the provider reported, because a breakdown that disagrees with the figure above
it reads as a bug even when the drift is one token. And a split that couldn't be measured
is absent rather than zeroed — a composition claiming no system prompt and no tools would
be a confident lie about the one thing this readout exists to expose.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import override

import pytest
from pydantic_ai import FunctionToolset, ModelMessage, ModelRequest, ModelResponse
from pydantic_ai.messages import TextPart, UserPromptPart
from pydantic_ai.usage import RequestUsage

from runs import TurnOverhead
from services.context_budget import compose
from services.conversations import ConversationStore
from services.registry import ModelRegistry

from ._helpers import app_state, client_app, collect_sse_events, patch_model_resolution
from ._json import arr, json_body, n, obj, s


def _messages(text: str) -> list[ModelMessage]:
    return [
        ModelRequest(parts=[UserPromptPart(content=text)]),
        ModelResponse(parts=[TextPart(content="ok")], usage=RequestUsage()),
    ]


# ── The sum ──────────────────────────────────────────────────────────────────────


def test_the_parts_add_up_to_the_providers_own_total():
    split = compose(1_000, TurnOverhead(system=400, tools=1_600), _messages("x" * 2_000))
    assert split is not None
    assert split.system + split.tools + split.messages == 1_000


@pytest.mark.parametrize("used", [1, 7, 33, 101, 999, 12_345, 262_144])
def test_they_add_up_at_every_size(used: int):
    """Three independently rounded shares can miss their total by a token or two; the
    largest part absorbs the drift. Sizes chosen to land on awkward remainders."""
    split = compose(used, TurnOverhead(system=333, tools=333), _messages("y" * 999))
    assert split is not None
    assert split.system + split.tools + split.messages == used


def test_proportions_follow_the_inputs():
    # Tools ten times the standing brief ⇒ ten times the share, give or take rounding.
    split = compose(10_000, TurnOverhead(system=100, tools=1_000), _messages(""))
    assert split is not None
    assert split.tools > split.system * 8


def test_a_thread_of_mostly_conversation_reads_that_way():
    """The distinction the readout exists for: a window filled by the conversation wants a
    compaction, one filled by tool schemas wants fewer tools on, and the total alone can't
    tell them apart."""
    split = compose(100_000, TurnOverhead(system=500, tools=500), _messages("z" * 400_000))
    assert split is not None
    assert split.messages > split.system + split.tools


# ── Absent, never zero ───────────────────────────────────────────────────────────


def test_no_overhead_measured_means_no_split():
    # A turn that never reached a model request has no tool definitions to have measured.
    assert compose(5_000, None, _messages("hello")) is None


def test_no_footprint_means_no_split():
    assert compose(None, TurnOverhead(system=10, tools=10), _messages("hello")) is None
    assert compose(0, TurnOverhead(system=10, tools=10), _messages("hello")) is None


def test_nothing_to_measure_means_no_split():
    # Everything empty: dividing by a zero total would raise, and a zeroed split would
    # claim the window holds nothing while `used` says otherwise.
    assert compose(5_000, TurnOverhead(system=0, tools=0), []) is None


# ── Measured off a real request ──────────────────────────────────────────────────


async def test_the_overhead_is_measured_from_the_request_that_went_out():
    """The measurement rides Pydantic AI's own `before_model_request` hook, so this pins
    that the hook's public fields carry the two things it needs — the standing brief, as
    instruction parts plus the system prompt on the outgoing messages, and the tool
    definitions the model was actually handed."""
    from dataclasses import dataclass

    from pydantic_ai import Agent, FunctionToolset, ModelRequestContext, RunContext
    from pydantic_ai.capabilities import AbstractCapability
    from pydantic_ai.models.test import TestModel

    from agent.overhead import measure_overhead

    toolset: FunctionToolset = FunctionToolset()

    @toolset.tool_plain(metadata={"sensitivity": "read"})
    def lookup(query: str, limit: int = 5) -> str:
        """Look something up in the corpus."""
        del query, limit
        return "x"

    seen: list[TurnOverhead] = []

    @dataclass
    class _Capture(AbstractCapability[object]):
        @override
        async def before_model_request(
            self, ctx: RunContext[object], request_context: ModelRequestContext
        ) -> ModelRequestContext:
            params = request_context.model_request_parameters
            seen.append(
                measure_overhead(
                    params.instruction_parts, request_context.messages, params.function_tools
                )
            )
            return request_context

    agent = Agent(
        TestModel(custom_output_text="ok", call_tools=[]),
        system_prompt="A" * 300,
        instructions="B" * 200,
        toolsets=[toolset],
        capabilities=[_Capture()],
    )
    _ = await agent.run("hello")

    assert seen, "before_model_request never fired"
    overhead = seen[0]
    # Both halves of the standing brief, counted together: the instruction parts off the
    # request parameters, the system prompt off the outgoing message list.
    assert overhead.system >= 500
    # The schema is serialized, so the tool's name, its docstring and its parameters are
    # all in there — the figure is far larger than the name alone.
    assert overhead.tools > len("lookup")
    assert overhead.tools > 100


def test_an_unnamed_instruction_part_falls_into_base():
    """Only a provider registered with a `name=` earns its own row. Our own literal
    instructions carry no name, and neither do the separators the library joins parts
    with, so both land in `base` — which is what keeps the blocks summing to the brief
    that was actually sent rather than to the sum of the named parts."""
    from pydantic_ai import InstructionPart
    from pydantic_ai.messages import AgentInstructionSource, InstructionId

    from agent.overhead import measure_overhead

    parts = [
        InstructionPart(content="B" * 200),
        InstructionPart(
            content="S" * 500,
            name="skill_catalog",
            id=InstructionId(AgentInstructionSource(), name="skill_catalog"),
        ),
    ]
    blocks = {b.id: b.chars for b in measure_overhead(parts, [], []).blocks}
    assert blocks["skill_catalog"] == 500
    # 200 for the unnamed part, plus the two characters `join` puts between them.
    assert blocks["base"] == 202

    # Nothing to measure is an empty measurement, not a crash.
    assert measure_overhead(None, [], []).blocks == ()


async def test_a_live_turn_reports_a_split_that_sums_to_its_footprint():
    """The whole chain: measured at the request node, carried on the Run, scaled in
    `compose`, and emitted on the metrics frame the gauge reads."""
    from pydantic_ai import FunctionToolset
    from pydantic_ai.models.test import TestModel

    from agent import build_chat_orchestrator
    from runs import RunRegistry, RunStatus

    toolset: FunctionToolset = FunctionToolset()

    @toolset.tool_plain(metadata={"sensitivity": "read"})
    def ping() -> str:
        """Answer with pong."""
        return "pong"

    orch = build_chat_orchestrator(
        "hi",
        model=TestModel(custom_output_text="ok", call_tools=[]),
        categories={"util": toolset},
        context_window=100_000,
    )
    run = RunRegistry().submit(kind="chat", owner_id="operator", orchestrator=orch)
    await run.wait()
    assert run.status is RunStatus.done

    frame = [e.body for e in run.stream.replay() if e.body.type == "run.metrics"][-1]
    assert frame.context is not None
    parts = frame.context.parts
    assert parts is not None, "a turn that made a real request must report a split"
    assert parts.system + parts.tools + parts.messages == frame.context.used
    # The tool the turn was given is in there; a zero would mean the schemas went
    # unmeasured while still being sent.
    assert parts.tools > 0
    assert parts.system > 0


# ── The itemisation ──────────────────────────────────────────────────────────────


def _overhead(
    *,
    blocks: Sequence[tuple[str, int]] = (),
    groups: Sequence[tuple[str, int, int]] = (),
) -> TurnOverhead:
    from runs import BriefBlock, ToolGroupOverhead

    brief = tuple(BriefBlock(id=i, chars=c) for i, c in blocks)
    tool_groups = tuple(ToolGroupOverhead(category=c, tools=t, chars=x) for c, t, x in groups)
    return TurnOverhead(
        system=sum(block.chars for block in brief),
        tools=sum(g.chars for g in tool_groups),
        blocks=brief,
        groups=tool_groups,
    )


def test_each_group_is_the_sum_of_its_own_segments():
    """The two resolutions describe the same tokens. A segment list that didn't add up to
    the bar above it would make the operator arbitrate between two of our own numbers."""
    split = compose(
        50_000,
        _overhead(
            blocks=[("base", 4_000), ("skill_catalog", 6_000)],
            groups=[("files", 8, 5_000), ("web", 2, 900)],
        ),
        _messages("x" * 20_000),
    )
    assert split is not None
    for total, group in (("system", "brief"), ("tools", "tools"), ("messages", "messages")):
        rows = [s.tokens for s in split.segments if s.group == group]
        assert rows, f"{group} carries weight, so it must carry rows"
        assert sum(rows) == getattr(split, total)
    assert sum(s.tokens for s in split.segments) == 50_000


def test_a_tool_category_carries_how_many_tools_put_it_there():
    """'22k of schemas' and '22k of schemas across 68 tools' lead to different decisions —
    the first looks like a lot of text, the second like a catalog to prune."""
    split = compose(
        10_000,
        _overhead(blocks=[("base", 500)], groups=[("external", 68, 30_000), ("builtin", 1, 200)]),
        _messages("x" * 1_000),
    )
    assert split is not None
    external = next(s for s in split.segments if s.id == "external")
    assert external.count == 68
    assert external.group == "tools"
    # A brief block or a message class has no population to report — a count of 1 there
    # would read as "one file", which is a claim we haven't measured.
    assert all(s.count is None for s in split.segments if s.group != "tools")


def test_rows_appear_only_once_they_weigh_something():
    """The list is not a roster with zeros in it. A thread that has called no tools has no
    tool-results row, and a catalog with no MCP servers connected has no row for them —
    each appears the moment it starts costing the window."""
    quiet = compose(
        10_000,
        _overhead(blocks=[("base", 900)], groups=[("builtin", 1, 300)]),
        _messages("hello" * 100),
    )
    assert quiet is not None
    assert {s.id for s in quiet.segments} == {"base", "builtin", "conversation"}

    from pydantic_ai.messages import ToolCallPart, ToolReturnPart

    busy = compose(
        10_000,
        _overhead(blocks=[("base", 900)], groups=[("builtin", 1, 300), ("external", 4, 2_000)]),
        [
            ModelRequest(parts=[UserPromptPart(content="find it")]),
            ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="external_search", args={"q": "x" * 400}, tool_call_id="1"
                    )
                ]
            ),
            ModelRequest(
                parts=[
                    ToolReturnPart(
                        tool_name="external_search",
                        content={"hits": ["y" * 2_000]},
                        tool_call_id="1",
                    )
                ]
            ),
        ],
    )
    assert busy is not None
    assert {"external", "tool_calls", "tool_results"} <= {s.id for s in busy.segments}


def test_a_row_that_rounds_to_nothing_is_not_shown():
    # It is still inside its group's total, which is where a token nobody can see belongs.
    split = compose(
        1_000,
        _overhead(blocks=[("base", 500_000), ("delegate", 1)], groups=[("builtin", 1, 400)]),
        _messages("x"),
    )
    assert split is not None
    assert all(s.tokens > 0 for s in split.segments)
    assert sum(s.tokens for s in split.segments) == 1_000


def test_an_overhead_with_no_detail_still_reports_its_totals():
    """The coarse reading must never depend on the fine one: an overhead measured before
    the itemisation existed, or one whose detail a library change put out of reach, still
    draws the bar."""
    split = compose(9_000, TurnOverhead(system=1_000, tools=3_000), _messages("x" * 4_000))
    assert split is not None
    assert split.system > 0 and split.tools > 0 and split.messages > 0
    assert split.system + split.tools + split.messages == 9_000
    # Two coarse rows plus the conversation — no invented detail.
    assert {s.id for s in split.segments} == {"base", "tools", "conversation"}


def test_message_weight_is_split_by_what_the_operator_can_do_about_it():
    """A thread heavy with tool results wants tools that return less; one heavy with
    conversation wants a compaction. The total can't tell them apart."""
    from pydantic_ai.messages import ToolReturnPart

    messages = [
        ModelRequest(parts=[UserPromptPart(content="find me everything" * 20)]),
        ModelRequest(
            parts=[
                ToolReturnPart(
                    tool_name="search", content={"hits": ["z" * 40_000]}, tool_call_id="1"
                )
            ]
        ),
    ]
    split = compose(10_000, _overhead(blocks=[("base", 100)], groups=[("web", 1, 100)]), messages)
    assert split is not None
    results = next(s for s in split.segments if s.id == "tool_results")
    conversation = next(s for s in split.segments if s.id == "conversation")
    assert results.tokens > conversation.tokens * 50


def test_the_standing_brief_is_not_counted_twice():
    """The system prompt sits at the head of the history *and* is part of the brief. Counted
    in both places it inflates the message row at every other row's expense — small, and
    exactly the kind of wrong a scaled split hides."""
    from pydantic_ai.messages import SystemPromptPart

    from services.conversation_view import message_chars

    with_brief = [
        ModelRequest(parts=[SystemPromptPart(content="B" * 5_000), UserPromptPart(content="hi")]),
    ]
    assert message_chars(with_brief).prose == len("hi")


async def test_a_providers_own_contribution_is_attributed_to_it():
    """What makes the brief actionable: 'your brief is 5k' is a fact, 'the skill catalog is
    4k of it' is a decision. Each provider is registered under a `name`, so the library
    stamps the part it resolves to and the row is read off the assembled request."""
    from pydantic_ai import RunContext
    from pydantic_ai.models.test import TestModel

    from agent import build_chat_orchestrator
    from runs import RunRegistry, RunStatus
    from tools import RunDeps

    async def skill_catalog_instructions(_ctx: RunContext[RunDeps]) -> str:
        return "S" * 4_000

    def delegate_instructions(_ctx: RunContext[RunDeps]) -> str:
        # Sync on purpose: features write both shapes, and a blanket `await` in the
        # measuring shim would have cost this turn rather than this row.
        return "D" * 1_000

    orch = build_chat_orchestrator(
        "hi",
        model=TestModel(custom_output_text="ok", call_tools=[]),
        categories={"util": FunctionToolset()},
        instruction_providers=[skill_catalog_instructions, delegate_instructions],
        context_window=100_000,
    )
    run = RunRegistry().submit(kind="chat", owner_id="operator", orchestrator=orch)
    await run.wait()
    assert run.status is RunStatus.done

    frame = [e.body for e in run.stream.replay() if e.body.type == "run.metrics"][-1]
    assert frame.context is not None and frame.context.parts is not None
    brief = {s.id: s.tokens for s in frame.context.parts.segments if s.group == "brief"}
    assert {"skill_catalog", "delegate"} <= brief.keys(), brief
    # The bigger provider reads bigger, and the fixed prompt is still its own row.
    assert brief["skill_catalog"] > brief["delegate"]
    assert brief.get("base", 0) > 0


# ── The reload ───────────────────────────────────────────────────────────────────


def test_a_stored_measurement_round_trips():
    """The column is a JSON blob, so the shape has to survive the trip in both
    directions — including the itemisation, which is the half the operator can act on."""
    from runs import BriefBlock, ToolGroupOverhead
    from runs import TurnOverhead as Overhead

    original = Overhead(
        system=300,
        tools=900,
        blocks=(BriefBlock(id="base", chars=200), BriefBlock(id="skill_catalog", chars=100)),
        groups=(ToolGroupOverhead(category="memory", tools=4, chars=900),),
    )
    assert Overhead.from_dict(original.as_dict()) == original


def test_an_unreadable_stored_measurement_reads_as_absent():
    """A blob a version skew (or a hand-edited row) left malformed shows no breakdown
    rather than a wrong one — the same absent-not-guessed rule the composition itself
    follows."""
    from runs import TurnOverhead as Overhead

    assert Overhead.from_dict(None) is None
    assert Overhead.from_dict("not a mapping") is None
    assert Overhead.from_dict({"tools": 200}) is None  # no `system`
    assert Overhead.from_dict({"system": 100, "tools": "lots"}) is None


def test_a_measurement_without_itemisation_still_reads():
    """A blob written before the segments existed carries only the two totals. The coarse
    three-way reading is worth having on its own, so it survives without them."""
    from runs import TurnOverhead as Overhead

    assert Overhead.from_dict({"system": 100, "tools": 200}) == Overhead(system=100, tools=200)


async def test_a_failed_measurement_leaves_the_stored_one_alone(tmp_path: Path):
    """A turn that never reached a model request measures nothing. That is ignored rather
    than written as absence: the previous figure still describes this thread better than
    nothing does, and blanking it would cost the operator the breakdown for a turn that
    didn't even run."""
    from core.db import init_db, make_engine
    from core.vault import Vault
    from runs import TurnOverhead as Overhead

    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    vault = Vault(tmp_path / "vault.json")
    await vault.setup("pw")
    store = ConversationStore(engine, vault)

    conversation_id = await store.create_conversation("operator")
    assert await store.get_overhead(conversation_id) is None

    await store.set_overhead(conversation_id, Overhead(system=100, tools=200))
    await store.set_overhead(conversation_id, None)
    assert await store.get_overhead(conversation_id) == Overhead(system=100, tools=200)


async def test_a_turn_leaves_its_measurement_on_its_own_thread(monkeypatch: pytest.MonkeyPatch):
    """What makes a reload able to break the window down at all — and the reason it is
    stored per conversation rather than remembered per mode.

    Neither the standing brief nor the tool schemas reach the message history, so a cold
    load has nothing to measure. Without this the operator would have to send a message to
    learn why their window is full, which is the decision they opened the breakdown to
    make. Storing it on the thread also means the split describes *this* conversation's
    request — a per-mode memory would hand one thread another's configuration."""
    patch_model_resolution(monkeypatch)
    async with client_app() as (client, app):
        created = json_body(await client.post("/chat", json={"prompt": "hello"}))
        _ = await collect_sse_events(client, s(created["run_id"]))
        conversation_id = s(created["conversation_id"])
        conversations = app_state(app, "conversations", ConversationStore)

        overhead = await conversations.get_overhead(conversation_id)
        assert overhead is not None, "a completed turn must leave its overhead behind"
        # Both halves are real: the app assembles a full tool catalog and a standing brief,
        # so a zero in either would mean the measurement found nothing while the request
        # plainly carried something.
        assert overhead.system > 0
        assert overhead.tools > 0
        # It belongs to the thread that ran the turn, and to no other. This is the whole
        # difference from the per-mode memory it replaces: that one would have handed this
        # second chat thread the first one's configuration.
        other = await conversations.create_conversation("operator")
        assert await conversations.get_overhead(other) is None


async def test_a_reopened_thread_reports_the_breakdown_without_another_turn(
    monkeypatch: pytest.MonkeyPatch,
):
    """The bug this exists to fix, end to end.

    Re-reading a conversation must serve the same itemised split the live stream did. The
    footprint it splits is recovered from the stored transcript; the overhead it splits it
    *with* has to come off the thread, because neither the standing brief nor the tool
    schemas are in that transcript. Before this was stored, a reopened thread could only
    report one undifferentiated figure until the operator sent another message — which is
    the decision they opened the breakdown to make."""
    from pydantic_ai.usage import RequestUsage

    from runs import BriefBlock, ToolGroupOverhead
    from runs import TurnOverhead as Overhead

    # The window is the gauge's denominator and has its own tests; stub it so this one is
    # about the split rather than about role resolution.
    async def main_context_window(_self: ModelRegistry, _owner_id: str) -> int | None:
        return 200_000

    monkeypatch.setattr(ModelRegistry, "main_context_window", main_context_window)

    async with client_app() as (client, app):
        store = app_state(app, "conversations", ConversationStore)
        conversation_id = await store.create_conversation("operator")
        # A settled turn carrying real provider usage — the stub model reports none, and
        # a thread with no measured footprint has no gauge to break down at all.
        _ = store.record(
            conversation_id,
            [
                ModelRequest(parts=[UserPromptPart(content="what is filling my window?")]),
                ModelResponse(
                    parts=[TextPart(content="a great many tool schemas")],
                    usage=RequestUsage(input_tokens=40_000, output_tokens=200),
                ),
            ],
        )
        await store.set_overhead(
            conversation_id,
            Overhead(
                system=4_000,
                tools=60_000,
                blocks=(BriefBlock(id="base", chars=4_000),),
                groups=(ToolGroupOverhead(category="external", tools=68, chars=60_000),),
            ),
        )

        context_value = json_body(await client.get(f"/conversations/{conversation_id}"))["context"]
        assert context_value is not None
        context = obj(context_value)
        parts_value = context["parts"]
        assert parts_value is not None, "a reopened thread must not fall back to one flat figure"
        parts = obj(parts_value)
        assert n(parts["system"]) > 0 and n(parts["tools"]) > 0
        # The split still agrees with the total printed above it, cold as well as warm.
        assert n(parts["system"]) + n(parts["tools"]) + n(parts["messages"]) == context["used"]
        # And the itemisation survives the round trip — it is the half the operator can
        # act on ("`external` is 60% of your window, across 68 tools" is a switch to find).
        external = [obj(seg) for seg in arr(parts["segments"]) if obj(seg)["id"] == "external"]
        assert external and external[0]["count"] == 68


async def test_a_reopened_thread_whose_endpoint_reported_no_usage_still_reads(
    monkeypatch: pytest.MonkeyPatch,
):
    """A local server that answers ``input_tokens=0`` is the case the estimate exists for.

    The live turn falls back to it and the gauge fills; a cold read of that same thread has
    to reach the same figure, or reopening the conversation blanks a ring the operator was
    watching a moment earlier and takes the breakdown with it."""
    from pydantic_ai.usage import RequestUsage

    from core.config import get_settings
    from runs import TurnOverhead as Overhead
    from services.conversation_view import estimate_footprint

    async def main_context_window(_self: ModelRegistry, _owner_id: str) -> int | None:
        return 200_000

    monkeypatch.setattr(ModelRegistry, "main_context_window", main_context_window)

    async with client_app() as (client, app):
        store = app_state(app, "conversations", ConversationStore)
        conversation_id = await store.create_conversation("operator")
        overhead = Overhead(system=4_000, tools=60_000)
        _ = store.record(
            conversation_id,
            [
                ModelRequest(parts=[UserPromptPart(content="x" * 2_000)]),
                ModelResponse(
                    parts=[TextPart(content="an answer nobody costed")],
                    usage=RequestUsage(input_tokens=0, output_tokens=0),
                ),
            ],
        )
        await store.set_overhead(conversation_id, overhead)

        detail = json_body(await client.get(f"/conversations/{conversation_id}"))
        expected = estimate_footprint(
            await store.history(conversation_id),
            overhead,
            fallback_overhead_tokens=get_settings().context_overhead_fallback_tokens,
        )
        assert detail["context"] is not None
        assert obj(detail["context"])["used"] == expected
        # And the readout beside the ring agrees with it, rather than reporting null.
        assert obj(detail["stats"])["context_used"] == expected


# ── What counts as message weight ────────────────────────────────────────────────


def test_a_structured_tool_result_counts_as_the_json_it_becomes():
    """The bug this readout surfaced. A tool result is usually a dict, and it reaches the
    model as serialized JSON — but the estimator read only `str` content, scoring every
    one of them zero. On a tool-heavy thread that is most of the window: the split
    credited the whole weight to the tool *schemas* rather than the results they
    returned, and auto-compaction (which shares the estimate) held off on exactly the
    threads filling up fastest."""
    from pydantic_ai.messages import ToolReturnPart

    from services.conversation_view import estimate_tokens

    payload = {"results": [{"title": "t" * 200, "snippet": "s" * 2_000} for _ in range(5)]}
    messages = [
        ModelRequest(parts=[ToolReturnPart(tool_name="search", content=payload, tool_call_id="1")])
    ]
    assert estimate_tokens(messages) > 2_000


def test_binary_content_still_counts_as_nothing():
    """The rule the fix above had to leave intact. A retained screenshot is base64 in the
    blob; measuring it by character length would read one image as hundreds of thousands
    of phantom tokens and compact a thread nowhere near full."""
    from pydantic_ai import BinaryContent
    from pydantic_ai.messages import ToolReturnPart

    from services.conversation_view import estimate_tokens

    image = BinaryContent(data=b"x" * 100_000, media_type="image/png")
    messages = [
        ModelRequest(parts=[ToolReturnPart(tool_name="shot", content=image, tool_call_id="1")])
    ]
    assert estimate_tokens(messages) == 0


def test_dict_keys_count_too():
    """Keys are serialized alongside the data and are a real share of a wide row's
    tokens — a result of many short values under long field names is mostly field
    names."""
    from pydantic_ai.messages import ToolReturnPart

    from services.conversation_view import estimate_tokens

    wide = {"a_long_field_name_here" * 4: "v", "another_long_field_name" * 4: "v"}
    messages = [ModelRequest(parts=[ToolReturnPart(tool_name="t", content=wide, tool_call_id="1")])]
    assert estimate_tokens(messages) > 40


def test_a_tool_calls_arguments_count_as_weight():
    """A call's arguments are JSON on the wire like any result. They used to score zero —
    the estimate read `content`, and a `ToolCallPart` carries `args` — which on a thread
    of many small calls is a real slice of the window credited to nothing at all."""
    from pydantic_ai.messages import ToolCallPart

    from services.conversation_view import estimate_tokens

    calls = [
        ModelResponse(
            parts=[
                ToolCallPart(
                    tool_name="fs_write",
                    args={"path": f"/projects/app/src/module_{i}.py", "content": "x" * 400},
                    tool_call_id=str(i),
                )
            ],
            usage=RequestUsage(),
        )
        for i in range(10)
    ]
    assert estimate_tokens(calls) > 900


def test_field_mode_reasoning_weighs_nothing_in_the_footprint():
    """Thinking the library read out of a provider field goes back into that field, and the
    Qwen, DeepSeek and GLM chat templates drop it when they render the prompt — so it costs
    the next request nothing.

    Counting it would be worse than the error it fixes: `should_compact` takes the *larger*
    of the provider's reported prompt size and this estimate, so an inflated estimate
    overrides an accurate server-reported figure on exactly the templates that strip. It
    stays in the *breakdown*, which answers a different question and has its own lever."""
    from pydantic_ai.messages import ThinkingPart

    from services.conversation_view import estimate_tokens, message_class_chars

    thinking = [
        ModelResponse(
            parts=[
                ThinkingPart(content="t" * 40_000, id="reasoning_content", provider_name="openai"),
                TextPart(content="ok"),
            ],
            usage=RequestUsage(),
        )
    ]
    assert estimate_tokens(thinking) == estimate_tokens(_messages("")[1:])
    assert message_class_chars(thinking)["reasoning"].prose == 40_000


def test_tag_mode_reasoning_does_weigh():
    """The correction. Thinking read out of the response's own `content` is re-wrapped in
    thinking tags and appended to the assistant message's **text** — there is no template
    hook that removes it, so the next request carries every character. The older claim that
    reasoning is "never re-sent" was wrong for this half, and on a thinking model working
    through a long thread the half is most of it."""
    from pydantic_ai.messages import ThinkingPart

    from services.conversation_view import estimate_tokens

    tagged = [
        ModelResponse(
            parts=[ThinkingPart(content="t" * 40_000, id="content"), TextPart(content="ok")],
            usage=RequestUsage(),
        )
    ]
    bare = [ModelResponse(parts=[TextPart(content="ok")], usage=RequestUsage())]

    assert estimate_tokens(tagged) > estimate_tokens(bare) + 8_000
    # A part with no id at all takes the same path, which is the shape a local server's
    # `<think>` block arrives as.
    unmarked = [
        ModelResponse(
            parts=[ThinkingPart(content="t" * 40_000), TextPart(content="ok")],
            usage=RequestUsage(),
        )
    ]
    assert estimate_tokens(unmarked) == estimate_tokens(tagged)


def test_the_standing_brief_is_counted_once_not_twice():
    """A `SystemPromptPart` sits at the head of the history *and* is measured by the
    turn's overhead record. Counting both would charge a thread twice for the one thing
    it can least reduce."""
    from pydantic_ai.messages import SystemPromptPart

    from services.conversation_view import estimate_tokens

    plain = [ModelRequest(parts=[UserPromptPart(content="hi")])]
    with_brief = [
        ModelRequest(parts=[SystemPromptPart(content="b" * 4_000), UserPromptPart(content="hi")])
    ]
    assert estimate_tokens(with_brief) == estimate_tokens(plain)


# ── The footprint: what the next request would actually weigh ────────────────────


def test_a_footprint_adds_the_measured_overhead_to_the_messages():
    """The trigger's number. `estimate_tokens` measures the conversation; a request is the
    conversation plus the brief plus every tool schema, and a threshold checked against
    the first alone is checked against a fraction of what gets sent."""
    from services.conversation_view import estimate_footprint, estimate_tokens

    messages = _messages("x" * 4_800)
    overhead = TurnOverhead(system=4_800, tools=4_100)  # ~1000 + ~1000 tokens
    footprint = estimate_footprint(messages, overhead, fallback_overhead_tokens=12_000)
    assert footprint == estimate_tokens(messages) + 2_000


def test_a_thread_with_no_overhead_record_assumes_a_catalog_rather_than_nothing():
    """The fallback, and the direction it leans. A thread whose turns all predate the
    per-thread overhead record has no measurement to read — and assuming zero would claim
    the request is smaller than it can possibly be, which is the one error a limit guard
    must not make. The configured guess stands in instead."""
    from services.conversation_view import estimate_footprint, estimate_tokens

    messages = _messages("hello")
    footprint = estimate_footprint(messages, None, fallback_overhead_tokens=12_000)
    assert footprint == estimate_tokens(messages) + 12_000


async def test_a_turn_that_parks_for_approval_records_its_overhead_before_resuming(
    tmp_path: Path,
):
    """A turn parked awaiting approval has already measured its request, so it records
    what that request weighs rather than waiting for the operator to decide.

    This is what keeps an approval-heavy thread from being the one kind that can never
    show a breakdown on reload — and it is why the resume path needs no write of its own:
    a resume re-runs the same configuration the park already measured."""
    from pydantic_ai import FunctionToolset
    from pydantic_ai.models.test import TestModel

    from agent import build_chat_orchestrator
    from core.db import init_db, make_engine
    from core.vault import Vault
    from runs import RunRegistry, RunStatus
    from tools import RunDeps

    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    vault = Vault(tmp_path / "vault.json")
    await vault.setup("pw")
    store = ConversationStore(engine, vault)
    conversation_id = await store.create_conversation("operator")

    toolset: FunctionToolset[RunDeps] = FunctionToolset()

    @toolset.tool_plain(requires_approval=True)
    def delete_thing(name: str) -> str:
        """Delete the named thing."""
        return f"deleted {name}"

    run = RunRegistry().submit(
        kind="chat",
        owner_id="operator",
        orchestrator=build_chat_orchestrator(
            "delete the thing",
            model=TestModel(custom_output_text="done"),
            categories={"danger": toolset},
            store=store,
            conversation_id=conversation_id,
        ),
    )
    await run.wait()
    assert run.status is RunStatus.awaiting_input

    overhead = await store.get_overhead(conversation_id)
    assert overhead is not None, "a parked turn has measured its request already"
    assert overhead.system > 0
    # The one tool it was given, itemised under its own category — the row the operator
    # would act on — beside the chassis's own `run_code`, which is a row of its own too.
    assert [(g.category, g.tools) for g in overhead.groups] == [("danger", 1), ("run_code", 1)]


def test_a_negative_figure_reads_as_absent():
    """These are character counts, so nothing below zero is a reading. Left in, one would
    flow through the composer's proportional scaling as a negative share and draw a bar
    segment of negative width — a wrong breakdown, which is worse than none."""
    from runs import TurnOverhead as Overhead

    assert Overhead.from_dict({"system": -1, "tools": 200}) is None
    assert (
        Overhead.from_dict({"system": 100, "tools": 200, "blocks": [{"id": "b", "chars": -5}]})
        is None
    )
    assert (
        Overhead.from_dict(
            {"system": 100, "tools": 200, "groups": [{"category": "c", "tools": -1, "chars": 5}]}
        )
        is None
    )


async def test_a_failed_overhead_write_never_fails_the_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The breakdown is a readout. If its write fails, the operator loses a reload's
    detail — they must not lose the answered turn, which is what letting the exception out
    would cost: the run would end `error` and its messages would route through the
    degraded error-flush instead of the finalize that already recorded them."""
    from pydantic_ai.models.test import TestModel

    from agent import build_chat_orchestrator
    from core.db import init_db, make_engine
    from core.vault import Vault
    from runs import RunRegistry, RunStatus

    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    vault = Vault(tmp_path / "vault.json")
    await vault.setup("pw")
    store = ConversationStore(engine, vault)
    conversation_id = await store.create_conversation("operator")

    async def boom(
        _self: ConversationStore, _conversation_id: str, _overhead: TurnOverhead | None
    ) -> None:
        raise RuntimeError("database is locked")

    monkeypatch.setattr(ConversationStore, "set_overhead", boom)

    run = RunRegistry().submit(
        kind="chat",
        owner_id="operator",
        orchestrator=build_chat_orchestrator(
            "hello",
            # No tool calls: the default catalog carries an approval-gated tool that
            # would park the run before it ever reached the write under test.
            model=TestModel(custom_output_text="hi", call_tools=[]),
            store=store,
            conversation_id=conversation_id,
        ),
    )
    await run.wait()

    assert run.status is RunStatus.done, "a readout's write must not decide the outcome"
    # And the turn itself recorded, through the normal finalize rather than a flush.
    assert len(await store.history(conversation_id)) == 2
