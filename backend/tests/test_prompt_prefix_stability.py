"""The head of a request must be the same bytes on turn two as on turn one.

Everything the prompt-cache work rests on is this one property. llama.cpp matches a
longest common prefix per slot, vLLM chains block hashes, and the OpenAI wire has a single
implicit breakpoint over the whole rendered prefix — so each of them reuses a request's
leading tokens only while they are identical to what it saw last, and a single byte moving
anywhere in the head re-reads the entire conversation behind it. On a long local thread that
is the difference between a turn starting immediately and a turn starting in fourteen
seconds.

`agent/prefix_watch.py` *reports* when that happens at runtime. These tests are what stop it
happening in the first place, by pinning the properties a future contributor could break
without any test noticing: a timestamp or a counter added to a head block, a set iterated
into the tool array, a static instruction registered after a dynamic one.

The agent is built twice throughout, because `build_agent` runs once per turn — a property
asserted over one agent's two requests would prove nothing about the next turn, which is
exactly where the expensive invalidations live.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from pydantic_ai import InstructionPart, RunContext
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

import agent.factory as factory
from agent.factory import build_agent
from runs import Run, RunStream
from tools import RunDeps, core_categories

OWNER = "operator"


def _capturing() -> tuple[FunctionModel, list[AgentInfo]]:
    """A model that answers immediately and keeps what it was handed."""
    seen: list[AgentInfo] = []

    def respond(_messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(info)
        return ModelResponse(parts=[TextPart("ok")])

    return FunctionModel(respond), seen


def _deps(**overrides) -> RunDeps:
    run = Run(id="t", kind="chat", owner_id=OWNER, stream=RunStream())
    return RunDeps(run=run, owner_id=OWNER, **overrides)


async def _head(*, providers=(), dormant=factory.NO_DORMANT, **deps) -> AgentInfo:
    """One turn's assembled request, from an agent built the way a turn builds one."""
    model, seen = _capturing()
    agent = build_agent(
        model,
        categories=core_categories(),
        instruction_providers=providers,
        dormant=dormant,
    )
    await agent.run("hi", deps=_deps(**deps))
    assert len(seen) == 1
    return seen[0]


def _parts(info: AgentInfo) -> list[InstructionPart]:
    return info.model_request_parameters.instruction_parts or []


def _brief(info: AgentInfo) -> str:
    return InstructionPart.join(_parts(info)) or ""


def _blocks(info: AgentInfo) -> dict[str, str]:
    return {
        part.id.name: part.content for part in _parts(info) if part.id is not None and part.id.name
    }


def _tool_names(info: AgentInfo) -> list[str]:
    return [tool.name for tool in info.function_tools]


@pytest.fixture
def frozen_clock(monkeypatch):
    """Pin the date block, so a test run that straddles midnight is not a failing test."""

    def freeze(moment: datetime) -> None:
        class _Clock(datetime):
            @classmethod
            def now(cls, tz=None):  # noqa: D102 - a stand-in for one classmethod
                return moment if tz is None else moment.astimezone(tz)

        monkeypatch.setattr(factory, "datetime", _Clock)

    freeze(datetime(2026, 9, 13, 11, 30).astimezone())
    return freeze


# --- the head across two turns of one thread -----------------------------------------


async def test_two_turns_of_one_thread_send_the_same_brief(frozen_clock):
    """The baseline, and the one that catches the widest class of regression: any provider
    that grows a timestamp, a counter, a random id or a set-ordered list will fail here
    without anyone having to think of that case in advance."""
    one = await _head()
    two = await _head()

    assert _brief(one) == _brief(two)
    assert _tool_names(one) == _tool_names(two)


async def test_the_tool_array_order_is_the_same_on_every_build():
    """Order, not membership. A provider renders the array in the order it is given, so the
    same tools in two orders are two different prefixes — and the catalog is assembled by
    walking mappings, which is exactly where an accidental `set` would hide."""
    assert _tool_names(await _head()) == _tool_names(await _head())


def _run_code(info: AgentInfo):
    return next(tool for tool in info.function_tools if tool.name == "run_code")


@pytest.mark.parametrize("level", ["manual", "edit", "auto", "yolo"])
async def test_run_code_is_the_same_bytes_on_every_turn_at_one_level(level):
    """`run_code`'s description lists the tools the thread's level clears unasked, so it is
    the one tool definition that moves when the level does — a price paid once, at the
    change. Within one level it must be byte-identical turn to turn: rendered from a set,
    or stamped with anything, it would re-read the conversation on every request."""
    one, two = _run_code(await _head(permission=level)), _run_code(await _head(permission=level))
    assert (one.description, one.parameters_json_schema) == (
        two.description,
        two.parameters_json_schema,
    )
    # And last, so a level change moves the tail of the tool array rather than its middle.
    assert _tool_names(await _head(permission=level))[-1] == "run_code"


async def test_the_same_withheld_set_spelled_two_ways_gives_one_array():
    """`disabled_tools` arrives as a set, and a set's iteration order must not reach the
    array. Cheap to pin and expensive to discover: the symptom is an operator's threads
    re-prefilling after a restart for no reason they could name."""
    forward = frozenset(["files_write_file", "code_execute"])
    backward = frozenset(["code_execute", "files_write_file"])

    one = await _head(disabled_tools=forward)
    two = await _head(disabled_tools=backward)

    assert _tool_names(one) == _tool_names(two)
    assert "files_write_file" not in _tool_names(one), (
        "nothing was withheld, so this proved nothing"
    )


async def test_only_the_date_block_moves_when_the_day_rolls(frozen_clock):
    """Midnight is the one head change this codebase accepts, and it has to be *only* that
    block: the date is registered last precisely so the roll falls outside the prefix
    Anthropic pins, and a second block moving with it would put the change in the middle."""
    before = _blocks(await _head())
    frozen_clock(datetime(2026, 9, 14, 0, 30).astimezone())
    after = _blocks(await _head())

    assert before["date"] != after["date"]
    assert {name: text for name, text in before.items() if name != "date"} == {
        name: text for name, text in after.items() if name != "date"
    }


async def test_a_level_change_moves_the_level_block_and_the_tool_array(frozen_clock):
    """One of the two mid-turn events that invalidate a whole thread — a Plan-level turn
    narrowing, or an approved plan widening it again. Worth pinning what it touches, because
    the head and the array moving *together* is what makes it expensive, and a change that
    quietly added a third would be invisible."""
    acting = await _head(permission="auto")
    planning = await _head(permission="plan")

    assert _blocks(acting)["level"] != _blocks(planning)["level"]
    assert set(_tool_names(planning)) < set(_tool_names(acting))
    moved = {name for name, text in _blocks(acting).items() if _blocks(planning).get(name) != text}
    assert moved == {"level"}, f"a level change also moved {moved - {'level'}}"


# --- the invariant Anthropic's breakpoint arithmetic depends on -----------------------


async def test_the_static_part_is_first_and_there_is_exactly_one(frozen_clock):
    """The one place block *order* pays, and the library will not tell you when it stops
    working.

    `pydantic_ai/_instructions.py` marks a part dynamic unless it was given as a literal
    string, so every block this factory registers is dynamic and only the literal brief is
    static. `models/anthropic.py` then places its instructions cache breakpoint by counting
    the statics and indexing that far in — `num_prefix_blocks + num_static - 1` — which
    assumes the statics lead. Register a static part after a dynamic one and the breakpoint
    silently lands on the wrong block: no error, no warning, and a messages-level cache miss
    then re-reads the expensive middle of the brief at full price.

    Asserted as the library's own `InstructionPart.sorted` being a no-op, which is the exact
    precondition, plus the count — because one static part in the wrong place and two static
    parts in the right one are different bugs.
    """
    parts = _parts(await _head())

    assert parts, "no instruction parts, so this asserted nothing"
    assert parts == InstructionPart.sorted(parts), (
        "the head is no longer sorted static-first, which moves Anthropic's instructions "
        "cache breakpoint onto the wrong block with no error anywhere"
    )
    statics = [part for part in parts if not part.dynamic]
    assert len(statics) == 1
    assert parts[0] is statics[0]


async def test_a_static_part_added_after_a_dynamic_one_is_caught(frozen_clock):
    """The negative half: the assertion above has to actually fail when the mistake is made,
    or it is a test that passes for the shape of the list rather than for its order."""
    parts = _parts(await _head())
    literal = next(part for part in parts if not part.dynamic)
    misordered = [*parts, literal]

    assert misordered != InstructionPart.sorted(misordered)


# --- what a feature may put in the head ----------------------------------------------


async def test_a_feature_provider_joins_the_head_without_disturbing_the_order(frozen_clock):
    """A manifest's own instructions are registered between the literal brief and the
    thread's own blocks. They must not break the static-first invariant, and the block they
    add must be stable across turns like every other."""

    def catalog_like(ctx: RunContext[RunDeps]) -> str:
        return "- alpha: the first one\n- beta: the second"

    one = await _head(providers=(catalog_like,))
    two = await _head(providers=(catalog_like,))

    parts = _parts(one)
    assert parts == InstructionPart.sorted(parts)
    assert _brief(one) == _brief(two)
    assert "alpha" in _blocks(one)["catalog_like"]


async def test_a_provider_that_says_nothing_adds_no_block(frozen_clock):
    """The no-op path every provider takes when its capability is unwired. An empty block
    still joined as a separator would be a byte in the head that moves the first time the
    feature is configured — for a feature the operator never turned on."""

    def silent(ctx: RunContext[RunDeps]) -> str:
        return ""

    assert _brief(await _head(providers=(silent,))) == _brief(await _head())


# --- a session-scoped block is head material, and carries nothing untrusted ----------


async def test_the_browser_brief_is_absent_until_a_session_exists(frozen_clock):
    """The browse brief is head material by the seam rule — byte-stable for as long as the
    session lives — and it resolves to nothing without one. So a thread that never opens a
    page pays nothing for it, and the turn where it appears is a turn whose tool array
    changed anyway.

    It also has to carry **nothing from a page**: head placement is only defensible while
    the block is harness text plus our own constant, and a fetched page's words arriving
    there would sit in front of the guardrails on the one seam a reconstructed history
    cannot reach.
    """
    from tools.browse import browse_instructions

    blocks = _blocks(await _head(providers=(browse_instructions,)))

    assert blocks.get("browse", "") == "", (
        "the browser brief resolved with no session wired, so a thread that never opens a "
        "page is paying for it in every request"
    )


def test_the_browser_brief_is_a_constant_with_no_page_in_it():
    """The other half, asserted over the text itself: whatever the brief says, it is written
    here. A provider that interpolated a URL, a title or a snapshot into it would be putting
    model-read content into the standing brief."""
    from prompts.browse import BROWSER_ADDENDUM

    assert "{" not in BROWSER_ADDENDUM and "}" not in BROWSER_ADDENDUM, (
        "the browser brief carries a format placeholder, so something is interpolated into "
        "the head — which is where the guardrails live"
    )


# --- the tail is where volatile content goes -----------------------------------------


async def test_nothing_in_the_head_changes_between_two_identical_turns_of_a_long_thread(
    frozen_clock,
):
    """The property stated the way an operator would: two turns in a row, nothing touched,
    and the server must see the same head. Distinct from the first test only in carrying a
    replayed history behind it, which is what a real second turn has and what a head change
    would throw away."""
    model, seen = _capturing()
    agent = build_agent(model, categories=core_categories())
    deps = _deps()
    first = await agent.run("hi", deps=deps)
    await agent.run("again", deps=deps, message_history=first.all_messages())

    assert len(seen) == 2
    assert _brief(seen[0]) == _brief(seen[1])
    assert _tool_names(seen[0]) == _tool_names(seen[1])
    # And the history really did grow, so this is the two-turn case and not the one-turn one
    # wearing its name.
    assert len(seen[1].model_request_parameters.instruction_parts or []) == len(_parts(seen[0]))
    assert len(first.all_messages()) >= 2
