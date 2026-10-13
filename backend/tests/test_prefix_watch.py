"""The prefix-cache diagnostic: does it see what an inference engine sees?

An instrument that reports the wrong cause is worse than no instrument, because the next
person spends their afternoon on the block it named. So these are mostly tests about
*honesty*: that a head change zeroes the reusable count rather than reporting the message
match anyway, that a wall-clock timestamp on a message part is not mistaken for a content
change, that the order of the tool array counts, and that an image is fingerprinted without
ever being rendered to a string.

The end-to-end half drives real turns through the agent the engine builds, because the whole
value of the reading is that the bytes hashed are the bytes that ship — after the system
prompt is reasserted and after the deferred schemas are gone. A unit test over hand-built
parts cannot assert that; only a turn can.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic_ai import ModelRequest, ToolDefinition, UserPromptPart
from pydantic_ai.messages import BinaryContent
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel

from agent import stream_agent_run
from agent.factory import NO_DORMANT, build_agent
from agent.prefix_watch import compare_prefix, digest_request, watch_prefix_enabled
from core.container import ServiceContainer
from runs import PrefixDigest, PrefixLedger, Run, RunStream
from tools import RunDeps, core_categories

OWNER = "operator"

#: A stand-in for what a manifest declares dormant, over a category the **core** catalog
#: actually contributes — the real dormant set is a feature manifest's, and a reveal of a
#: category these tests never assembled would withhold nothing and bring nothing back. Which
#: groups this installation withholds is `test_tool_search.py`'s business, not this file's.
DORMANT = {"tasks": "the running checklist"}


def _tool(name: str, description: str = "does a thing") -> ToolDefinition:
    return ToolDefinition(
        name=name, description=description, parameters_json_schema={"type": "object"}
    )


def _digest(**kwargs) -> PrefixDigest:
    """A fingerprint with everything defaulted except what a given test varies."""
    return PrefixDigest(**{"instructions": "brief", **kwargs})


# --- the fingerprint itself ----------------------------------------------------------


def test_a_parts_timestamp_is_not_a_content_change():
    """The failure mode that would have made the whole instrument useless: a
    `UserPromptPart` stamps itself with the wall clock, so a fingerprint over every
    attribute would report a divergence on every single request — and blame the head for
    its own noise."""
    now = datetime.now(UTC)
    early = ModelRequest(parts=[UserPromptPart("hello", timestamp=now)])
    late = ModelRequest(parts=[UserPromptPart("hello", timestamp=now + timedelta(hours=3))])

    assert digest_request(None, [], [early]).messages == digest_request(None, [], [late]).messages


def test_the_content_itself_still_counts():
    """...and the guard above has not been bought by ignoring the content."""
    one = ModelRequest(parts=[UserPromptPart("hello")])
    two = ModelRequest(parts=[UserPromptPart("hello there")])

    assert digest_request(None, [], [one]).messages != digest_request(None, [], [two]).messages


def test_an_image_is_fingerprinted_from_its_bytes():
    """A prompt's content list can hold a two-megabyte image. It has to be *hashed*, never
    serialized — and it still has to be distinguished, or an operator swapping one screenshot
    for another would read as an unchanged prefix."""
    blue = BinaryContent(data=b"\x89PNG-blue", media_type="image/png")
    red = BinaryContent(data=b"\x89PNG-red!", media_type="image/png")
    one = ModelRequest(parts=[UserPromptPart(["look", blue])])
    two = ModelRequest(parts=[UserPromptPart(["look", red])])

    assert digest_request(None, [], [one]).messages != digest_request(None, [], [two]).messages


def test_the_tool_array_order_is_part_of_the_fingerprint():
    """A provider renders the array in the order it is given, so the same tools in two
    orders are two different prefixes. A set-based reading would call that unchanged and
    send the next reader looking somewhere else entirely."""
    tools = [_tool("a_one"), _tool("a_two")]

    forward = digest_request(None, tools, [])
    backward = digest_request(None, list(reversed(tools)), [])

    assert forward.tools != backward.tools
    # ...and the *names* it reports are order-sensitive too, so the reading and the
    # fingerprint can never disagree about what was offered.
    assert forward.tool_names == ("a_one", "a_two")
    assert backward.tool_names == ("a_two", "a_one")


def test_a_tools_description_counts_as_much_as_its_name():
    """Rewording a description is a full head change on every engine with one cache
    boundary, which is all of them but Anthropic. A fingerprint over names alone would miss
    the day someone edits a docstring."""
    before = digest_request(None, [_tool("a_one", "old words")], [])
    after = digest_request(None, [_tool("a_one", "new words")], [])

    assert before.tools != after.tools


# --- the comparison ------------------------------------------------------------------


def test_the_first_request_has_nothing_to_compare_against():
    verdict = compare_prefix(None, _digest(messages=("m1", "m2")))

    assert verdict.head_changed == "first"
    assert verdict.divergence_kind == "first"
    assert verdict.reused_messages == 0
    # Still reports what it carried, so the line is legible on the very first turn.
    assert verdict.total_messages == 2


def test_more_messages_behind_an_unchanged_head_is_a_clean_append():
    previous = _digest(messages=("m1",))
    current = _digest(messages=("m1", "m2", "m3"))

    verdict = compare_prefix(previous, current)

    assert verdict.head_changed == "none"
    assert verdict.divergence_kind == "append"
    assert verdict.divergence_index is None
    assert verdict.reused_messages == 1
    assert verdict.reused_everything


def test_a_head_change_zeroes_the_reusable_count():
    """The one reading this instrument exists to get right. The messages still match, and
    an engine still re-reads every one of them, because they sit behind a brief that moved.
    Reporting "reused 1" on a turn that paid for everything would be the exact wrong
    answer."""
    previous = _digest(instructions="old", messages=("m1",))
    current = _digest(instructions="new", messages=("m1", "m2"))

    verdict = compare_prefix(previous, current)

    assert verdict.head_changed == "instructions"
    assert verdict.reused_messages == 0
    assert not verdict.reused_everything
    # The structural fact is still reported — it is true, and it is what says the *only*
    # thing that moved was the head.
    assert verdict.divergence_kind == "append"


def test_the_changed_block_is_named():
    """`head=instructions` identifies a category; `head=instructions(level)` identifies a
    cause. The naming is the entire diagnostic value over what `ttft_ms` already said."""
    previous = _digest(instructions="old", blocks=(("skill_catalog", "s1"), ("level", "L1")))
    current = _digest(instructions="new", blocks=(("skill_catalog", "s1"), ("level", "L2")))

    verdict = compare_prefix(previous, current)

    assert verdict.changed_blocks == ("level",)
    assert "level" in verdict.summary()


def test_a_block_that_appears_or_vanishes_is_named_too():
    """A cache sees a byte sequence, not a set of features, so a block arriving is the same
    event as a block being rewritten — and a block *leaving* has to be named, or the reading
    would be "the joined brief moved and no block did", which points at the separators."""
    previous = _digest(instructions="old", blocks=(("browse", "b1"),))
    current = _digest(instructions="new", blocks=(("skill_catalog", "s1"),))

    assert compare_prefix(previous, current).changed_blocks == ("skill_catalog", "browse")


def test_only_the_last_message_changing_reads_as_the_tail_seam():
    """The per-turn-boundary cost this codebase accepts by design: turn N's request ends
    with the volatile context appended to the operator's prompt, and turn N+1 replays that
    prompt without it. Named rather than merely indexed, so it is distinguishable from a
    bug at a glance."""
    previous = _digest(messages=("m1", "m2+tail"))
    current = _digest(messages=("m1", "m2", "a2", "m3"))

    verdict = compare_prefix(previous, current)

    assert verdict.divergence_kind == "tail"
    assert verdict.divergence_index == 1
    assert verdict.reused_messages == 1


def test_a_shorter_history_that_still_matches_is_a_clean_drop():
    """A rewind, a regenerate or an edit. Costs nothing — a shorter matching prefix is
    still a matching prefix — so it must not read as a divergence."""
    previous = _digest(messages=("m1", "m2", "m3"))
    current = _digest(messages=("m1", "m2"))

    verdict = compare_prefix(previous, current)

    assert verdict.divergence_kind == "clean_drop"
    assert verdict.divergence_index is None
    assert verdict.reused_messages == 2
    assert verdict.reused_everything


def test_a_replaced_head_and_a_shorter_history_reads_as_a_fold():
    previous = _digest(messages=("m1", "m2", "m3", "m4", "m5"))
    current = _digest(messages=("m1", "summary", "m5"))

    verdict = compare_prefix(previous, current)

    assert verdict.divergence_kind == "fold"
    assert verdict.divergence_index == 1


def test_a_divergence_inside_the_history_reads_as_a_rewrite():
    """Nothing in this codebase should produce one, which is why it is worth being able to
    see: a history processor that edited an earlier turn would."""
    previous = _digest(messages=("m1", "m2", "m3", "m4", "m5"))
    current = _digest(messages=("m1", "m2", "edited", "m4", "m5"))

    assert compare_prefix(previous, current).divergence_kind == "rewrite"


def test_a_reveal_reads_as_tools_added():
    previous = _digest(tools="t1", tool_names=("files_read",))
    current = _digest(tools="t2", tool_names=("files_read", "browse_open", "browse_click"))

    verdict = compare_prefix(previous, current)

    assert verdict.head_changed == "tools"
    assert verdict.tools_added == ("browse_open", "browse_click")
    assert verdict.tools_removed == ()
    assert "tools+=2" in verdict.summary()


# --- the ledger ----------------------------------------------------------------------


def test_the_ledger_hands_back_what_a_conversation_last_sent():
    ledger = PrefixLedger()
    ledger.remember("conv-1", _digest(instructions="one"))
    ledger.remember("conv-2", _digest(instructions="two"))

    assert ledger.recall("conv-1").instructions == "one"
    assert ledger.recall("conv-2").instructions == "two"
    assert ledger.recall("conv-3") is None
    # A turn with no conversation (a stateless eval) neither records nor recalls.
    ledger.remember(None, _digest(instructions="orphan"))
    assert ledger.recall(None) is None


def test_the_ledger_forgets_the_least_recently_used_conversation():
    ledger = PrefixLedger(limit=2)
    ledger.remember("a", _digest(instructions="a"))
    ledger.remember("b", _digest(instructions="b"))
    # Touching `a` makes `b` the oldest, so the third arrival evicts `b` and not `a`.
    ledger.recall("a")
    ledger.remember("c", _digest(instructions="c"))

    assert ledger.recall("a") is not None
    assert ledger.recall("b") is None
    assert ledger.recall("c") is not None


# --- through a real turn -------------------------------------------------------------


def _deps(*, conversation_id: str | None = None, ledger: PrefixLedger | None = None) -> RunDeps:
    run = Run(id="t", kind="chat", owner_id=OWNER, stream=RunStream())
    caps = ServiceContainer()
    if ledger is not None:
        caps.add(ledger)
    return RunDeps(run=run, owner_id=OWNER, conversation_id=conversation_id, caps=caps)


async def _drive(model: FunctionModel, deps: RunDeps, *, dormant=NO_DORMANT) -> Run:
    """One real turn on the agent the engine actually builds. ``dormant`` defaults to
    nothing withheld, so a test that is not about deferral gets the whole catalog on step 1
    and the reveal test is the only one paying for a second tool array."""
    agent = build_agent(model, categories=core_categories(), dormant=dormant)
    async with agent.iter("go", deps=deps) as agent_run:
        await stream_agent_run(agent_run, deps.run)
    return deps.run


def _calls_then_answers(seen: list[AgentInfo], tool: str, args: str = "{}") -> FunctionModel:
    """Calls one tool, then answers — so the second request replays the first one's
    messages plus the round trip, which is the shape every step inside a turn has.

    A ``stream_function`` because the engine streams every request, and a ``FunctionModel``
    with only a non-streaming half asserts rather than answering.
    """

    async def stream_fn(_messages, info: AgentInfo):
        seen.append(info)
        if len(seen) == 1:
            yield {0: DeltaToolCall(name=tool, json_args=args)}
        else:
            yield "done"

    return FunctionModel(stream_function=stream_fn)


async def test_a_second_step_of_one_turn_reuses_the_whole_prefix():
    """The property that makes the *within*-turn reading trustworthy, and the baseline every
    other reading is judged against: the brief and the tool array are resolved once in the
    prelude, so steps 2..k of a turn differ from step 1 only by appended messages."""
    seen: list[AgentInfo] = []
    run = await _drive(_calls_then_answers(seen, "tasks_read"), _deps())

    assert len(seen) == 2, "the turn took one step, so there was no second prefix to compare"
    verdict = run.prefix_verdict
    assert verdict is not None
    assert verdict.head_changed == "none"
    assert verdict.divergence_kind == "append"
    assert verdict.reused_everything


async def test_revealing_a_group_moves_the_tool_array_and_nothing_else():
    """Cause 1's signature event, asserted through a real reveal rather than by
    construction. The brief must not move with it: the dormant index is built from the
    installation's declarations, not from what this turn has loaded, so a reveal that also
    rewrote the index would double the invalidation for no gain."""
    steps: list[AgentInfo] = []
    model = _calls_then_answers(steps, "search_tools", '{"queries": ["tasks"]}')

    run = await _drive(model, _deps(), dormant=DORMANT)

    assert len(steps) == 2, "the reveal never happened, so this asserted nothing"
    assert not any(t.name.startswith("tasks_") for t in steps[0].function_tools)
    assert any(t.name.startswith("tasks_") for t in steps[1].function_tools)
    verdict = run.prefix_verdict
    assert verdict is not None
    assert verdict.head_changed == "tools"
    assert verdict.changed_blocks == ()
    assert verdict.tools_added, "a reveal that added no tools is not a reveal"
    assert verdict.reused_messages == 0


async def test_the_ledger_carries_the_reading_across_a_turn_boundary():
    """What no per-turn object can do, and where the expensive invalidations live. Two
    separate runs, one conversation, one ledger: the second turn's first request knows what
    the first turn last sent, so a head change between turns is attributable instead of
    reported as `first`."""
    ledger = PrefixLedger()
    first = await _drive(
        _calls_then_answers([], "tasks_read"), _deps(conversation_id="c1", ledger=ledger)
    )
    assert first.prefix_verdict is not None

    second = await _drive(
        _calls_then_answers([], "tasks_read"), _deps(conversation_id="c1", ledger=ledger)
    )

    verdict = second.prefix_verdict
    assert verdict is not None
    assert verdict.head_changed != "first", "the ledger did not reach the second turn"
    assert verdict.head_changed == "none", "nothing in the head should move between turns"


async def test_without_a_ledger_each_turn_starts_cold_and_still_watches_within_itself():
    """The degraded path — a stateless eval, or a test that builds its own bag. The
    within-turn reading, which is what catches a reveal and a level move, has to survive
    it; only the cross-turn half is lost."""
    seen: list[AgentInfo] = []
    run = await _drive(_calls_then_answers(seen, "tasks_read"), _deps(conversation_id="c1"))

    assert len(seen) == 2
    verdict = run.prefix_verdict
    assert verdict is not None
    assert verdict.head_changed == "none"
    assert verdict.divergence_kind == "append"


async def test_the_watch_is_on_by_default_and_switching_it_off_silences_it(monkeypatch):
    """On by default, because a diagnostic nobody switched on is a diagnostic that is not
    there the one time it was needed. The escape hatch is asserted through a real turn
    rather than by inspecting the capability list — an instrument that is registered but
    reports nothing, or deregistered and still reporting, would pass a list check."""
    assert watch_prefix_enabled()
    assert (await _drive(_calls_then_answers([], "tasks_read"), _deps())).prefix_verdict is not None

    monkeypatch.setenv("HEPHAESTUS_PREFIX_WATCH", "0")
    assert not watch_prefix_enabled()
    quiet = await _drive(_calls_then_answers([], "tasks_read"), _deps())
    assert quiet.prefix_verdict is None


@pytest.mark.parametrize("value", ["0", "false", "OFF", "no"])
def test_every_spelling_of_off_is_honoured(monkeypatch, value: str):
    monkeypatch.setenv("HEPHAESTUS_PREFIX_WATCH", value)
    assert not watch_prefix_enabled()


async def test_the_providers_own_cache_verdict_stays_silent_on_a_local_endpoint():
    """The complement to the structural watch, and the property that makes registering it
    free: the harness's `WarnOnCacheBusts` reads `cache_read_tokens`, which is the ground
    truth where it exists and is absent on every local engine and on MLX.

    So it covers exactly the hosted half the structural watch cannot confirm, and it has to
    add *nothing* on the local half — where a warning per turn would be pure noise about a
    figure nobody reported."""
    import warnings

    from pydantic_ai_harness.warn_on_cache_busts import CacheBustWarning

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        await _drive(_calls_then_answers([], "tasks_read"), _deps())

    assert not [w for w in caught if issubclass(w.category, CacheBustWarning)]
