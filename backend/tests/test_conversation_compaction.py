"""Conversation compaction — folding older turns into a summary the thread's own model
writes.

The product's **one** context reduction, and the only one that fires on measured pressure —
the pressure-blind reductions (tool-result digesting, the attachment inline cap, the sandbox
output trim) are gone. What these guard, in order of how much damage getting them wrong
would do:

- **The persistence index.** ``agent/finalize.py`` records a turn as
  ``result.all_messages()[start:]``, with ``start = len(model_history)`` measured in
  ``agent/prelude.py`` after every history rewrite. The replay view
  reorders the tree, so if its length ever stopped matching what it represents, every turn
  after a compaction would persist the wrong slice.
- **Branch safety.** A checkpoint grafted onto a reseated leaf would re-parent the incoming
  answer out of its own version set, silently breaking version switching.
- **The boundary.** A fold takes everything since the newest checkpoint and nothing
  before it, so an earlier summary is absorbed rather than re-exposed as a turn.
- **Containment.** The summary is a real message row; it must not reach the listing
  preview, the message count, cross-chat search, or a transcript read.
"""

from __future__ import annotations

import pytest
from pydantic_ai import (
    BinaryContent,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RequestUsage

from agent import build_chat_orchestrator
from agent.factory import build_agent
from agent.history import merge_consecutive_requests
from agent.summarize import (
    CompactionOutcome,
    FoldFailed,
    build_auto_compact_policy,
    compact_conversation,
    should_compact,
    summarize_history,
)
from agent.turn import turn_deps
from core.config import Settings, get_settings
from prompts.compaction import COMPACT_PREAMBLE
from routes.deps import OPERATOR_ID
from runs import Run, RunStatus, RunStream, TurnOverhead
from services.conversation_view import estimate_tokens, project_tree

from ._helpers import client_app, fold_aware_model, fold_with, patch_model_resolution


def _turn(prompt: str, answer: str) -> list:
    return [
        ModelRequest(parts=[UserPromptPart(content=prompt)]),
        ModelResponse(parts=[TextPart(content=answer)]),
    ]


def _texts(messages: list) -> list[str]:
    """Every message's flattened text, for asserting on shape without part surgery."""
    out = []
    for message in messages:
        for part in message.parts:
            content = getattr(part, "content", None)
            if isinstance(content, str):
                out.append(content)
    return out


async def _compact(store, cid: str, *, summary: str = "SUMMARY"):
    """Run a compaction with a fixed summary — the boundary logic under test, not the model."""
    plan = await store.compaction_plan(cid)
    if plan is None:
        return None
    return store.record_compaction(
        cid,
        summary=summary,
        through_id=plan.through_id,
        expected_leaf_id=plan.expected_leaf_id,
        reason="threshold",
    )


# --- the replay view ---------------------------------------------------------


async def test_a_fold_covers_the_whole_thread_and_the_replay_is_the_summary_alone():
    """`compaction_plan` cuts at `len(path)`, so the fold covers the *whole* thread and the
    model's replay is the summary alone — and the *full* history is untouched, since three
    callers depend on it."""
    async with client_app() as (_client, app):
        store = app.state.conversations
        cid = await store.create_conversation(OPERATOR_ID)
        for i in range(4):
            store.record(cid, _turn(f"q{i}", f"a{i}"))

        plan = await store.compaction_plan(cid)
        assert plan is not None
        assert len(plan.messages) == 8  # every message on the path, q0/a0 … q3/a3
        assert await _compact(store, cid) is not None

        assert _texts(await store.model_history(cid)) == ["SUMMARY"]
        # And nothing was destroyed — the checkpoint is appended, not substituted, so the
        # operator's transcript still has all of it.
        assert _texts(await store.history(cid))[:8] == [
            "q0",
            "a0",
            "q1",
            "a1",
            "q2",
            "a2",
            "q3",
            "a3",
        ]


async def test_a_single_turn_thread_folds_whole():
    """One prompt the model answered by filling the window is the thread that most needs
    a fold."""
    async with client_app() as (_client, app):
        store = app.state.conversations
        cid = await store.create_conversation(OPERATOR_ID)
        store.record(cid, _turn("q0", "a0"))

        plan = await store.compaction_plan(cid)
        assert plan is not None and len(plan.messages) == 2


async def test_a_second_compaction_absorbs_the_first():
    """A second fold summarizes the *first summary* plus what followed — never the
    original turns a second time, which would defeat the point of the first pass, and never
    the older summary re-exposed as a plain turn."""
    async with client_app() as (_client, app):
        store = app.state.conversations
        cid = await store.create_conversation(OPERATOR_ID)
        for i in range(2):
            store.record(cid, _turn(f"q{i}", f"a{i}"))
        assert await _compact(store, cid, summary="FIRST") is not None
        for i in range(2, 4):
            store.record(cid, _turn(f"q{i}", f"a{i}"))

        plan = await store.compaction_plan(cid)
        assert plan is not None
        assert _texts(plan.messages) == ["FIRST", "q2", "a2", "q3", "a3"]
        assert await _compact(store, cid, summary="SECOND") is not None
        assert _texts(await store.model_history(cid)) == ["SECOND"]


async def test_model_history_is_unchanged_without_a_checkpoint():
    async with client_app() as (_client, app):
        store = app.state.conversations
        cid = await store.create_conversation(OPERATOR_ID)
        store.record(cid, _turn("q", "a"))
        assert _texts(await store.model_history(cid)) == _texts(await store.history(cid))


async def test_the_replay_view_survives_a_cold_reload():
    """`compacted`/`compacted_through` must rehydrate from the DB, or a restart would
    silently replay the whole folded history again."""
    async with client_app() as (_client, app):
        store = app.state.conversations
        cid = await store.create_conversation(OPERATOR_ID)
        for i in range(4):
            store.record(cid, _turn(f"q{i}", f"a{i}"))
        assert await _compact(store, cid) is not None

        warm = _texts(await store.model_history(cid))
        await store._worker.join()
        store._cache.clear()
        assert _texts(await store.model_history(cid)) == warm


async def test_the_persistence_index_still_selects_exactly_the_new_turn():
    """`start = len(model_history)` is what the engine slices a turn out of
    `all_messages()` at. The replay view reorders the tree, so this is the assertion
    that reordering never broke the arithmetic."""
    async with client_app() as (_client, app):
        store = app.state.conversations
        cid = await store.create_conversation(OPERATOR_ID)
        for i in range(4):
            store.record(cid, _turn(f"q{i}", f"a{i}"))
        assert await _compact(store, cid) is not None

        replayed = await store.model_history(cid)
        start = len(replayed)
        # What a turn hands back: the history it was given plus its own new messages.
        all_messages = [*replayed, *_turn("q4", "a4")]
        store.record(cid, all_messages[start:])

        await store._worker.join()
        store._cache.clear()
        # The new turn landed once, in order, on the active path — not duplicated and
        # not truncated.
        assert _texts(await store.history(cid))[-2:] == ["q4", "a4"]
        assert _texts(await store.model_history(cid)) == ["SUMMARY", "q4", "a4"]


# --- boundary selection ------------------------------------------------------


async def test_nothing_to_compact_only_when_nothing_follows_the_checkpoint():
    async with client_app() as (_client, app):
        store = app.state.conversations
        cid = await store.create_conversation(OPERATOR_ID)
        assert await store.compaction_plan(cid) is None
        store.record(cid, _turn("q0", "a0"))
        assert await _compact(store, cid) is not None
        assert await store.compaction_plan(cid) is None


async def test_compaction_folds_while_the_leaf_is_a_branch_point():
    """A regenerate has reseated the leaf and its run hasn't recorded yet — the one moment
    compaction is *most* needed, since the turn about to re-run is the one that overflowed.
    It folds: the checkpoint lands under the reseated leaf, the new answer hangs off the
    checkpoint, and the version set reads through it (see
    `tests/test_compaction_branch_points.py` for the full branch behaviour)."""
    async with client_app() as (_client, app):
        store = app.state.conversations
        cid = await store.create_conversation(OPERATOR_ID)
        for i in range(4):
            store.record(cid, _turn(f"q{i}", f"a{i}"))
        assert await store.compaction_plan(cid) is not None

        answer = [m for m in await store.messages_view(cid) if m.role == "assistant"][-1]
        assert await store.regenerate_point(cid, answer.id)
        assert await _compact(store, cid) is not None

        # The reseated request is still the end of the replay, so the regenerate that
        # follows re-answers the operator rather than the summary.
        assert _texts(await store.model_history(cid)) == ["SUMMARY", "q3"]

        store.record(cid, [ModelResponse(parts=[TextPart(content="a3-again")])])
        assert _texts(await store.model_history(cid))[-1] == "a3-again"


async def test_record_compaction_refuses_a_stale_plan():
    """Summarizing takes seconds, and the conversation claim blocks runs but not a version
    switch — so the leaf is re-checked at record time and a moved one drops the summary."""
    async with client_app() as (_client, app):
        store = app.state.conversations
        cid = await store.create_conversation(OPERATOR_ID)
        for i in range(4):
            store.record(cid, _turn(f"q{i}", f"a{i}"))
        plan = await store.compaction_plan(cid)
        assert plan is not None

        # The operator regenerates while the summary is being written.
        answer = [m for m in await store.messages_view(cid) if m.role == "assistant"][-1]
        assert await store.regenerate_point(cid, answer.id)

        assert (
            store.record_compaction(
                cid,
                summary="SUMMARY",
                through_id=plan.through_id,
                expected_leaf_id=plan.expected_leaf_id,
                reason="threshold",
            )
            is None
        )


# --- the threshold -----------------------------------------------------------


#: A thread whose brief and tool schemas *were* measured and came to nothing. Distinct
#: from `None`, which means "never measured" and makes the trigger assume the shipped
#: catalog's several thousand tokens rather than zero.
_NO_OVERHEAD = TurnOverhead(system=0, tools=0)


def _response(input_tokens: int, output_tokens: int = 0) -> ModelResponse:
    return ModelResponse(
        parts=[TextPart(content="x")],
        usage=RequestUsage(input_tokens=input_tokens, output_tokens=output_tokens),
    )


@pytest.mark.parametrize(
    ("used", "expected"),
    [(9_000, False), (9_500, True), (9_900, True)],
)
def test_should_compact_measures_against_the_window(used: int, expected: bool):
    # A measured (and here, empty) overhead, so this reads the reported footprint against
    # the threshold and nothing else — the unmeasured-overhead fallback has its own tests.
    assert should_compact([_response(used)], 10_000, 0.95, overhead=_NO_OVERHEAD) is expected


def test_should_compact_declines_without_a_declared_window():
    """No ceiling to measure against — compacting on a guess would fold a thread that was
    never under pressure."""
    assert should_compact([_response(999_999)], None, 0.95) is False


def test_should_compact_falls_back_to_an_estimate_when_usage_is_unreported():
    """Local servers commonly report `input_tokens=0`, which `context_footprint` treats as
    unmeasured. Without the estimate the feature would be dead on exactly that setup."""
    # Comfortably over the threshold at the prose rate `estimate_tokens` converts at.
    big = ModelRequest(parts=[UserPromptPart(content="x" * 60_000)])
    assert should_compact(
        [big, ModelResponse(parts=[TextPart(content="y")])], 10_000, 0.95, overhead=_NO_OVERHEAD
    )


def test_the_estimate_ignores_binary_content():
    """A retained inline image is base64 in the blob; measuring it by length would read one
    screenshot as hundreds of thousands of phantom tokens."""
    image = BinaryContent(data=b"\x00" * 100_000, media_type="image/png")
    message = ModelRequest(parts=[UserPromptPart(content=["hi", image])])
    assert estimate_tokens([message]) < 10


# --- the summarizer ----------------------------------------------------------
#
# How the summary request is built, and what a side run may and may not do, lives in
# `test_compaction_summarizer`; what stays here is the fold's contract with the store and
# the run.


async def test_summarize_history_degrades_to_a_failed_fold_on_a_model_error():
    """A model outage must leave the thread uncompacted, never fail the turn it was about
    to make room for."""

    class _Boom(TestModel):
        async def request_stream(self, *args, **kwargs):
            raise RuntimeError("model down")
            yield  # pragma: no cover

    run = Run(id="fold", kind="chat", owner_id=OPERATOR_ID, stream=RunStream())
    result = await summarize_history(
        build_agent(_Boom(), categories={}),
        turn_deps(run),
        [ModelRequest(parts=[UserPromptPart(content="hi")])],
    )
    assert result == FoldFailed("error")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("<think>weighing it up</think>the story so far", "the story so far"),
        ("<THINK>casing is the template's choice</think>\n\nthe story so far", "the story so far"),
        # A model that stops mid-thought emits an *unclosed* block, whose partial content
        # Pydantic AI still returns. Left in, the model's scratch reasoning would become
        # the thread's standing memory.
        ("the story so far\n<think>still reasoning when it stopped", "the story so far"),
    ],
)
async def test_summarize_history_strips_a_leaked_think_block(raw: str, expected: str):
    """A runtime that inlines its chain-of-thought in the content rather than as a thinking
    part. The summary is what the model replays for the rest of the thread, so a leaked
    block must never survive into it."""
    summary = await summarize_history(
        messages=[ModelRequest(parts=[UserPromptPart(content="hi")])], **fold_with(raw)
    )
    assert summary == expected


async def test_summarize_history_fails_when_only_reasoning_came_back():
    """A reply that is *nothing but* a think block leaves no summary — better to skip the
    compaction than to store an empty checkpoint the model would replay as its memory."""
    summary = await summarize_history(
        messages=[ModelRequest(parts=[UserPromptPart(content="hi")])],
        **fold_with("<think>never got to the answer</think>"),
    )
    assert summary == FoldFailed("summarizer_empty")


async def test_compact_conversation_end_to_end():
    async with client_app() as (_client, app):
        store = app.state.conversations
        cid = await store.create_conversation(OPERATOR_ID)
        for i in range(4):
            store.record(cid, _turn(f"q{i}", f"a{i}"))

        outcome = await compact_conversation(
            store,
            cid,
            reason="threshold",
            **fold_with("the story so far"),
        )
        assert isinstance(outcome, CompactionOutcome)
        # The stored summary is labelled, so the model can't read it as the operator's own
        # words once the provider merges it with the next turn's prompt — and the event
        # carries the same string the divider renders on a reload.
        assert outcome.summary == f"{COMPACT_PREAMBLE}\n\nthe story so far"
        assert outcome.messages_compacted == 8  # q0/a0 … q3/a3
        assert _texts(await store.model_history(cid))[0] == outcome.summary


async def test_the_outcome_reports_what_the_fold_cost():
    """The divider says "N messages folded, ~X → ~Y" — so the backend has to measure both
    ends. Coarse `estimate_tokens` figures, the same proxy the trigger measures with."""
    async with client_app() as (_client, app):
        store = app.state.conversations
        cid = await store.create_conversation(OPERATOR_ID)
        for i in range(4):
            store.record(cid, _turn(f"q{i} {'padding ' * 200}", f"a{i} {'padding ' * 200}"))

        outcome = await compact_conversation(
            store,
            cid,
            reason="threshold",
            **fold_with("short"),
        )
        assert isinstance(outcome, CompactionOutcome)
        assert outcome.messages_compacted == 8
        # The fold has to have actually bought room, and the numbers must be real.
        assert outcome.tokens_before > outcome.tokens_after > 0
        assert outcome.tokens_after == estimate_tokens(
            [ModelRequest(parts=[UserPromptPart(content=outcome.summary)])]
        )


async def test_the_cold_read_divider_reports_the_same_figures_as_the_event():
    """A live client renders the divider from `conversation.compacted`; a reload renders
    it from the projection. Both compute the numbers the same way, so they must agree —
    the figure must not change under the operator when they refresh the page."""
    async with client_app() as (_client, app):
        store = app.state.conversations
        cid = await store.create_conversation(OPERATOR_ID)
        for i in range(4):
            store.record(cid, _turn(f"q{i} {'padding ' * 200}", f"a{i} {'padding ' * 200}"))

        outcome = await compact_conversation(
            store,
            cid,
            reason="threshold",
            **fold_with("short"),
        )
        assert isinstance(outcome, CompactionOutcome)

        divider = next(v for v in await store.messages_view(cid) if v.role == "compaction")
        assert divider.id == outcome.message_id
        assert divider.messages_compacted == outcome.messages_compacted
        assert divider.tokens_before == outcome.tokens_before
        assert divider.tokens_after == outcome.tokens_after
        # And an ordinary turn carries no fold stats at all.
        user = next(v for v in await store.messages_view(cid) if v.role == "user")
        assert (user.messages_compacted, user.tokens_before, user.tokens_after) == (0, 0, 0)


async def test_the_fold_reason_survives_a_cold_read():
    """The one fact on the divider that cannot be re-derived from the messages: *why* the
    chassis folded. It is written onto the checkpoint's own metadata rather than a column,
    so the assertion that matters is that it comes back after the in-memory tree is gone —
    a reload must name the same cause the operator watched arrive."""
    async with client_app() as (_client, app):
        store = app.state.conversations
        cid = await store.create_conversation(OPERATOR_ID)
        for i in range(4):
            store.record(cid, _turn(f"q{i}", f"a{i}"))

        outcome = await compact_conversation(
            store,
            cid,
            reason="overflow",
            **fold_with("short"),
        )
        assert isinstance(outcome, CompactionOutcome)
        assert outcome.reason == "overflow"

        warm = next(v for v in await store.messages_view(cid) if v.role == "compaction")
        assert warm.compaction_reason == "overflow"

        # …and again off the sealed blob, with nothing left in memory to read it from.
        await store._worker.join()
        store._cache.clear()
        cold = next(v for v in await store.messages_view(cid) if v.role == "compaction")
        assert cold.compaction_reason == "overflow"
        # An ordinary turn has no reason to report, on either path.
        assert all(
            v.compaction_reason is None
            for v in await store.messages_view(cid)
            if v.role != "compaction"
        )


async def test_a_checkpoint_written_before_reasons_reads_as_none():
    """Every fold already in an operator's database predates the metadata, and there is no
    migration to backfill something nobody recorded. The projection has to answer "unknown"
    rather than guess the most common trigger — the divider drops the segment, where a
    wrong one would tell them the provider forced a fold they asked for."""
    legacy = ModelRequest(parts=[UserPromptPart(content="SUMMARY")])
    assert legacy.metadata is None
    views = project_tree([("chk", legacy)], compacted_ids=frozenset({"chk"}))
    assert [v.role for v in views] == ["compaction"]
    assert views[0].compaction_reason is None


async def test_a_second_folds_stats_cover_only_what_it_folded():
    """A second compaction stands in for the first summary plus what followed it — not the
    original turns all over again. Its divider must say so, on both paths."""
    async with client_app() as (_client, app):
        store = app.state.conversations
        cid = await store.create_conversation(OPERATOR_ID)
        for i in range(2):
            store.record(cid, _turn(f"q{i}", f"a{i}"))
        first = await compact_conversation(
            store,
            cid,
            reason="threshold",
            **fold_with("first"),
        )
        for i in range(2, 5):
            store.record(cid, _turn(f"q{i}", f"a{i}"))
        second = await compact_conversation(
            store,
            cid,
            reason="threshold",
            **fold_with("second"),
        )
        assert first is not None and second is not None
        assert first.messages_compacted == 4  # q0/a0/q1/a1
        assert second.messages_compacted == 7  # the first summary + q2/a2/q3/a3/q4/a4

        dividers = [v for v in await store.messages_view(cid) if v.role == "compaction"]
        assert [d.messages_compacted for d in dividers] == [4, 7]
        assert [d.tokens_before for d in dividers] == [first.tokens_before, second.tokens_before]


# --- containment -------------------------------------------------------------


async def test_a_checkpoint_stays_out_of_the_listing_search_and_transcript():
    """The summary is a real row. It must not become the sidebar preview (it is the tip
    right after a compaction), must not inflate the message count, must not be recalled by
    cross-chat search, and must not read back as something the assistant said."""
    async with client_app() as (_client, app):
        store = app.state.conversations
        cid = await store.create_conversation(OPERATOR_ID)
        for i in range(4):
            store.record(cid, _turn(f"q{i}", f"a{i}"))
        before = await store.get_summary(cid, OPERATOR_ID)
        assert await _compact(store, cid, summary="A DISTINCTIVE SUMMARY")

        after = await store.get_summary(cid, OPERATOR_ID)
        assert after.preview == before.preview  # still the last real message
        assert after.message_count == before.message_count

        await store._worker.join()
        store._cache.clear()
        cold = await store.get_summary(cid, OPERATOR_ID)
        assert "DISTINCTIVE" not in (cold.preview or "")

        search = app.state.conversation_search
        hits = await search.search(OPERATOR_ID, "DISTINCTIVE SUMMARY", limit=10)
        assert all("DISTINCTIVE" not in hit.snippet for hit in hits)

        transcript = await search.read(OPERATOR_ID, cid)
        assert "DISTINCTIVE" not in transcript.text


async def test_the_divider_renders_above_the_request_a_branch_point_fold_left_standing():
    """Transcript order, not tree order: at a branch point the checkpoint is appended
    under the reseated request but shown where the fold happened, or it would claim to
    have folded the request still waiting on its answer."""
    async with client_app() as (_client, app):
        store = app.state.conversations
        cid = await store.create_conversation(OPERATOR_ID)
        for i in range(4):
            store.record(cid, _turn(f"q{i}", f"a{i}"))
        answer = [m for m in await store.messages_view(cid) if m.role == "assistant"][-1]
        assert await store.regenerate_point(cid, answer.id)
        assert await _compact(store, cid) is not None

        views = await store.messages_view(cid)
        roles = [v.role for v in views]
        assert roles.count("compaction") == 1
        divider = roles.index("compaction")
        assert views[divider].content == "SUMMARY"
        # Everything before the divider is what was folded; q3 is still standing below it.
        assert [v.content for v in views[:divider] if v.role == "user"] == ["q0", "q1", "q2"]
        assert [v.content for v in views[divider + 1 :] if v.role == "user"] == ["q3"]


# --- the engine trigger ------------------------------------------------------


def _loaded_turn(prompt: str, answer: str, input_tokens: int) -> list:
    """A turn whose answer reports a real prompt size, so `context_footprint` can read it."""
    return [
        ModelRequest(parts=[UserPromptPart(content=prompt)]),
        ModelResponse(
            parts=[TextPart(content=answer)],
            usage=RequestUsage(input_tokens=input_tokens, output_tokens=10),
        ),
    ]


async def _run_turn(app, cid: str, *, policy):
    """Drive one real chat turn through the orchestrator against a seeded conversation."""
    orch = build_chat_orchestrator(
        "next question",
        # The summary is written by the turn's own model, so one model answers both.
        model=fold_aware_model(answer="the answer", summary="FOLDED AWAY"),
        categories={},
        store=app.state.conversations,
        conversation_id=cid,
        context_window=10_000,
        auto_compact=policy,
    )
    run = app.state.runs.submit(kind="chat", owner_id=OPERATOR_ID, orchestrator=orch)
    await run.wait()
    return run


async def test_a_full_thread_compacts_before_the_turn_and_announces_it():
    async with client_app() as (_client, app):
        store = app.state.conversations
        cid = await store.create_conversation(OPERATOR_ID)
        for i in range(3):
            store.record(cid, _loaded_turn(f"q{i}", f"a{i}", 100))
        store.record(cid, _loaded_turn("q3", "a3", 9_600))  # 96% of a 10k window

        run = await _run_turn(app, cid, policy=build_auto_compact_policy(Settings()))
        assert run.status is RunStatus.done

        events = [e.body for e in run.stream.replay() if e.body.type == "conversation.compacted"]
        assert len(events) == 1
        assert events[0].summary.endswith("FOLDED AWAY")
        assert events[0].summary.startswith(COMPACT_PREAMBLE)
        assert events[0].conversation_id == cid
        assert events[0].messages_compacted > 0
        # The event carries the cost of the fold, so the divider can state it without the
        # client counting or estimating anything itself. These fixtures fake *reported
        # usage* over near-empty text, so only the summary's own estimate is meaningful
        # here — the real before/after relationship is covered above, over real text.
        assert events[0].tokens_after == estimate_tokens(
            [ModelRequest(parts=[UserPromptPart(content=events[0].summary)])]
        )
        assert events[0].tokens_before >= 0

        # The model's view now opens on the summary, and the turn that ran persisted
        # normally on top of it.
        replayed = _texts(await store.model_history(cid))
        assert replayed[0].endswith("FOLDED AWAY")
        assert replayed[-2:] == ["next question", "the answer"]


async def test_a_store_failure_during_compaction_does_not_take_the_turn_down(monkeypatch):
    """Compaction is an optimization. If the store fails mid-fold the turn must still
    answer on the full history — the run's own context stop is the real backstop."""
    async with client_app() as (_client, app):
        store = app.state.conversations
        cid = await store.create_conversation(OPERATOR_ID)
        for i in range(3):
            store.record(cid, _loaded_turn(f"q{i}", f"a{i}", 100))
        store.record(cid, _loaded_turn("q3", "a3", 9_600))

        async def boom(*args, **kwargs):
            raise RuntimeError("store is having a day")

        monkeypatch.setattr(type(store), "compaction_plan", boom)
        run = await _run_turn(app, cid, policy=build_auto_compact_policy(Settings()))
        assert run.status is RunStatus.done
        assert not [e for e in run.stream.replay() if e.body.type == "conversation.compacted"]


async def test_a_thread_below_the_threshold_is_left_alone():
    async with client_app() as (_client, app):
        store = app.state.conversations
        cid = await store.create_conversation(OPERATOR_ID)
        for i in range(4):
            store.record(cid, _loaded_turn(f"q{i}", f"a{i}", 100))  # 1% of the window
        # This thread's brief and schemas are measured and empty, so the trigger reads the
        # 1% and nothing else. Left unmeasured it would assume the shipped catalog, which
        # alone overruns the deliberately tiny window these fixtures use.
        await store.set_overhead(cid, TurnOverhead(system=0, tools=0))

        run = await _run_turn(app, cid, policy=build_auto_compact_policy(Settings()))
        assert run.status is RunStatus.done
        assert not [e for e in run.stream.replay() if e.body.type == "conversation.compacted"]
        assert "FOLDED AWAY" not in _texts(await store.model_history(cid))


async def test_a_disabled_policy_never_compacts_however_full():
    async with client_app() as (_client, app):
        store = app.state.conversations
        cid = await store.create_conversation(OPERATOR_ID)
        for i in range(3):
            store.record(cid, _loaded_turn(f"q{i}", f"a{i}", 100))
        store.record(cid, _loaded_turn("q3", "a3", 9_900))

        run = await _run_turn(app, cid, policy=build_auto_compact_policy(Settings(), enabled=False))
        assert run.status is RunStatus.done
        assert not [e for e in run.stream.replay() if e.body.type == "conversation.compacted"]


async def test_the_turn_after_a_compaction_persists_exactly_itself():
    """The engine's `start = len(model_history)` slice, exercised through the real
    orchestrator: the compacted turn must add its own two messages to the tree and no more."""
    async with client_app() as (_client, app):
        store = app.state.conversations
        cid = await store.create_conversation(OPERATOR_ID)
        for i in range(3):
            store.record(cid, _loaded_turn(f"q{i}", f"a{i}", 100))
        store.record(cid, _loaded_turn("q3", "a3", 9_600))
        before = len(await store.history(cid))

        run = await _run_turn(app, cid, policy=build_auto_compact_policy(Settings()))
        assert run.status is RunStatus.done

        await store._worker.join()
        store._cache.clear()
        after = _texts(await store.history(cid))
        # +1 checkpoint, +1 user request, +1 response. Nothing re-recorded, nothing dropped.
        assert len(after) == before + 3
        assert after[-2:] == ["next question", "the answer"]


async def test_the_plans_anchor_names_the_rendered_turn_the_divider_follows():
    """A live client addresses turns, not tree nodes — an assistant turn can span several
    nodes and renders as one bubble. The anchor must be the id that bubble carries, or the
    divider would land somewhere a reload disagrees with."""
    async with client_app() as (_client, app):
        store = app.state.conversations
        cid = await store.create_conversation(OPERATOR_ID)
        for i in range(4):
            store.record(cid, _turn(f"q{i}", f"a{i}"))

        plan = await store.compaction_plan(cid)
        assert plan is not None
        assert await _compact(store, cid) is not None

        views = await store.messages_view(cid)
        divider = next(i for i, v in enumerate(views) if v.role == "compaction")
        assert views[divider - 1].id == plan.anchor_id


# --- the merge normalization the index depends on ----------------------------


def test_consecutive_requests_merge_so_the_index_stays_honest():
    """Pydantic AI collapses adjacent requests when it prepares the wire format, so
    ``all_messages()`` comes back shorter than what was handed in. Normalizing first is
    what keeps ``start`` pointing at the real boundary."""
    merged = merge_consecutive_requests(
        [
            ModelRequest(parts=[UserPromptPart(content="a")]),
            ModelRequest(parts=[UserPromptPart(content="b")]),
            ModelResponse(parts=[TextPart(content="r")]),
            ModelRequest(parts=[UserPromptPart(content="c")]),
        ]
    )
    assert len(merged) == 3
    assert [p.content for p in merged[0].parts] == ["a", "b"]


async def test_the_library_really_does_merge_and_its_own_index_is_not_a_substitute():
    """Two facts this normalization rests on, checked against the installed library
    rather than assumed.

    First, Pydantic AI genuinely merges adjacent requests: hand it a history ending in
    two of them and ``all_messages()`` comes back one short, so a naive
    ``all_messages()[len(history):]`` slices past the operator's own prompt.

    Second — the reason ``start`` isn't simply replaced by ``new_messages()`` — the
    library's index is per *agent run*, and one of our turns can be several (a
    continuation, an approval resume, a verifier correction). It is right about the run
    it describes; it just isn't answering the question ``start`` answers.
    """
    from pydantic_ai import Agent
    from pydantic_ai.models.function import FunctionModel

    def reply(messages, info):
        return ModelResponse(parts=[TextPart(content="ok")])

    history = [
        ModelRequest(parts=[UserPromptPart(content="first")]),
        ModelResponse(parts=[TextPart(content="answered")]),
        # What a checkpoint in front of the request a branch-point fold left standing
        # produces.
        ModelRequest(parts=[UserPromptPart(content="checkpoint")]),
        ModelRequest(parts=[UserPromptPart(content="standing")]),
    ]
    naive_start = len(history)

    result = await Agent(FunctionModel(reply)).run("new question", message_history=history)
    everything = result.all_messages()

    # The merge is real, and the naive index loses the operator's message because of it.
    assert len(everything) == len(history) + 1  # four in, five out — two became one
    assert not any(
        isinstance(part, UserPromptPart) and part.content == "new question"
        for message in everything[naive_start:]
        for part in message.parts
    )
    # Normalizing first is what fixes it: `start` measured against the merged list points
    # at the operator's prompt, which is what gets persisted.
    normalized = merge_consecutive_requests(history)
    assert isinstance(everything[len(normalized)], ModelRequest)
    assert any(
        isinstance(part, UserPromptPart) and part.content == "new question"
        for part in everything[len(normalized)].parts
    )


def test_the_merge_does_not_mutate_the_messages_it_was_given():
    """The store's in-memory tree shares these objects — mutating one in place would
    corrupt the durable history of every later read."""
    first = ModelRequest(parts=[UserPromptPart(content="a")])
    merge_consecutive_requests([first, ModelRequest(parts=[UserPromptPart(content="b")])])
    assert [p.content for p in first.parts] == ["a"]


async def test_a_steered_turn_replays_without_losing_the_next_turns_prompt():
    """The same index bug, reached the other way: a mid-run steering message persists as
    its own request directly behind the tool-return request it was injected into, so the
    *next* turn's history contains adjacent requests. Before normalization those merged
    inside `all_messages()` and the next turn persisted one message short — losing the
    operator's own prompt."""
    async with client_app() as (_client, app):
        store = app.state.conversations
        cid = await store.create_conversation(OPERATOR_ID)
        store.record(
            cid,
            [
                ModelRequest(parts=[UserPromptPart(content="do a thing")]),
                ModelResponse(parts=[ToolCallPart(tool_name="t", args={}, tool_call_id="c1")]),
                ModelRequest(parts=[ToolReturnPart(tool_name="t", content="r", tool_call_id="c1")]),
                ModelRequest(parts=[UserPromptPart(content="actually, also this")]),
                ModelResponse(parts=[TextPart(content="done")]),
            ],
        )
        run = await _run_turn(app, cid, policy=build_auto_compact_policy(Settings()))
        assert run.status is RunStatus.done

        await store._worker.join()
        store._cache.clear()
        assert _texts(await store.history(cid))[-2:] == ["next question", "the answer"]


# --- policy + routes ---------------------------------------------------------


def test_the_policy_resolves_from_config_defaults():
    policy = build_auto_compact_policy(get_settings())
    assert policy.enabled is True
    # 80%, not 95%: a fold at 95% leaves no room for the turn that triggered it.
    assert policy.threshold == pytest.approx(0.80)


def test_the_policy_takes_operator_overrides():
    policy = build_auto_compact_policy(Settings(), enabled=False, threshold=0.5)
    assert policy.enabled is False
    assert policy.threshold == pytest.approx(0.5)


async def test_chat_settings_round_trip_the_auto_compact_preferences():
    async with client_app() as (client, _app):
        body = await (await client.get("/chat/settings")).aread()
        assert b"auto_compact_enabled" in body

        resp = await client.put(
            "/chat/settings", json={"auto_compact_enabled": False, "auto_compact_threshold": 0.8}
        )
        assert resp.status_code == 200
        assert resp.json()["auto_compact_enabled"] is False
        assert resp.json()["auto_compact_threshold"] == pytest.approx(0.8)

        # A PUT touching only one group leaves the rest alone.
        resp = await client.put("/chat/settings", json={"agent_request_limit": 33})
        assert resp.json()["auto_compact_threshold"] == pytest.approx(0.8)
        assert resp.json()["auto_compact_enabled"] is False


@pytest.mark.parametrize("threshold", [0, 1.5, -0.2])
async def test_an_out_of_range_threshold_is_rejected(threshold: float):
    """0 would fire on an empty thread; above 1 it could never fire at all."""
    async with client_app() as (client, _app):
        resp = await client.put("/chat/settings", json={"auto_compact_threshold": threshold})
        assert resp.status_code == 422


async def test_the_per_conversation_override_round_trips():
    async with client_app() as (client, app):
        cid = await app.state.conversations.create_conversation(OPERATOR_ID)
        resp = await client.get(f"/conversations/{cid}/auto-compact")
        assert resp.json() == {"override": None, "effective": True}

        resp = await client.put(f"/conversations/{cid}/auto-compact", json={"override": False})
        assert resp.json() == {"override": False, "effective": False}

        # Clearing it goes back to inheriting the operator default.
        resp = await client.put(f"/conversations/{cid}/auto-compact", json={"override": None})
        assert resp.json() == {"override": None, "effective": True}


async def test_the_override_is_scoped_to_one_thread():
    """The per-thread switch is exactly that — turning folding off in one conversation
    must not reach into another, which still inherits the operator's default."""
    async with client_app() as (client, app):
        store = app.state.conversations
        muted = await store.create_conversation(OPERATOR_ID)
        other = await store.create_conversation(OPERATOR_ID)
        await client.put(f"/conversations/{muted}/auto-compact", json={"override": False})
        assert (await client.get(f"/conversations/{other}/auto-compact")).json() == {
            "override": None,
            "effective": True,
        }


async def _fold_now(client, app, cid: str):
    """Press "compact now" and wait for the run it starts to settle, answering with it.

    A hand-started fold is a run, so the route answers ``202`` with an id and *everything*
    worth asserting on — the events, and why it declined — is on the run rather than on
    that response. The wait is the run's own task, which is the real thing rather than a
    poll: a settled status that was reached by sleeping long enough would pass on a fold
    that never started."""
    resp = await client.post(f"/conversations/{cid}/compact")
    assert resp.status_code == 202, resp.text
    run = app.state.runs.get(resp.json()["run_id"])
    assert run is not None
    if run.task is not None:
        await run.task
    return run


def _emitted(run) -> list[str]:
    """The event type of every frame this run put on its stream, in order."""
    return [event.body.type for event in run.stream.replay(0)]


def _fold_frames(run) -> list[str]:
    """Just the frames the *fold* emitted, in order — not the run lifecycle around them.

    A submitted run brackets its orchestrator with `run.started`/`run.ended`, which is a
    fact about runs and not about folding. Comparing those too would make the two triggers
    differ for a reason that has nothing to do with what is being compared."""
    return [t for t in _emitted(run) if t.startswith(("compaction.", "conversation.compacted"))]


async def test_manual_compact_folds_and_announces_itself(monkeypatch):
    """The fold the operator asked for emits exactly what an automatic one does, because
    it is now the same code path — and it says *they* asked for it, not the threshold,
    which this trigger ignores entirely."""
    async with client_app() as (client, app):
        patch_model_resolution(monkeypatch, output_text="a hand-made summary")
        store = app.state.conversations
        cid = await store.create_conversation(OPERATOR_ID)
        for i in range(4):
            store.record(cid, _turn(f"q{i}", f"a{i}"))

        run = await _fold_now(client, app, cid)
        assert run.status is RunStatus.done, run.detail
        assert "compaction.started" in _emitted(run)
        assert "conversation.compacted" in _emitted(run)
        landed = next(
            e.body for e in run.stream.replay(0) if e.body.type == "conversation.compacted"
        )
        assert landed.reason == "manual"

        # And the checkpoint a reload reads names the same cause the live event did.
        detail = (await client.get(f"/conversations/{cid}")).json()
        divider = next(m for m in detail["messages"] if m["role"] == "compaction")
        assert divider["compaction_reason"] == "manual"


async def test_manual_compact_blocks_when_there_is_nothing_to_fold(monkeypatch):
    # A fold takes everything since the newest checkpoint, so "nothing to fold" means
    # nothing has been said — here, an empty thread.
    async with client_app() as (client, app):
        patch_model_resolution(monkeypatch)
        cid = await app.state.conversations.create_conversation(OPERATOR_ID)
        run = await _fold_now(client, app, cid)
        assert run.status is RunStatus.blocked
        assert "compaction.started" not in _emitted(run)


async def test_manual_compact_folds_a_single_turn_thread(monkeypatch):
    """The button has no prerequisite. A single prompt the model answered by filling the
    window is exactly the moment the operator most needs to fold."""
    async with client_app() as (client, app):
        patch_model_resolution(monkeypatch)
        store = app.state.conversations
        cid = await store.create_conversation(OPERATOR_ID)
        store.record(cid, _turn("q0", "a0"))

        run = await _fold_now(client, app, cid)
        assert run.status is not RunStatus.blocked, run.detail
        assert "conversation.compacted" in _emitted(run)


async def test_a_fold_that_had_something_to_fold_and_failed_says_so(monkeypatch):
    """The summarizer writing nothing is not the same answer as the thread having nothing
    to fold, and reporting both the same way is what makes a genuine failure look like a
    no-op. A thread that plainly *has* foldable turns must report the failure as one — and
    it must have announced the fold first, since it got far enough to try."""
    async with client_app() as (client, app):
        patch_model_resolution(monkeypatch, output_text="")
        store = app.state.conversations
        cid = await store.create_conversation(OPERATOR_ID)
        for i in range(4):
            store.record(cid, _turn(f"q{i}", f"a{i}"))

        run = await _fold_now(client, app, cid)
        assert run.status is RunStatus.blocked
        assert "did not land" in run.detail
        # The distinguishing half: this one got past the plan, so it announced itself and
        # then failed — where "nothing to fold" never announces at all.
        assert "compaction.started" in _emitted(run)
        assert "conversation.compacted" not in _emitted(run)


async def test_manual_compact_409s_on_a_busy_conversation(monkeypatch):
    """It appends to the tree, so unlike retitle it must not run beside a live turn — or
    beside a request that has repositioned the leaf but not finished acting on it, which
    is what the claim (and not the live-run check) is what covers."""
    async with client_app() as (client, app):
        patch_model_resolution(monkeypatch)
        cid = await app.state.conversations.create_conversation(OPERATOR_ID)
        app.state.runs.claim(cid, OPERATOR_ID)
        try:
            assert (await client.post(f"/conversations/{cid}/compact")).status_code == 409
        finally:
            app.state.runs.release(cid, OPERATOR_ID)


async def test_manual_compact_404s_for_an_unknown_conversation(monkeypatch):
    async with client_app() as (client, _app):
        patch_model_resolution(monkeypatch)
        assert (await client.post("/conversations/nope/compact")).status_code == 404


async def test_manual_compact_is_written_by_the_threads_main_model(monkeypatch):
    """The operator's fold is the thread's own model continuing its own conversation, like
    an automatic one — never the utility binding, which would read the thread cold on a
    model that holds none of its prefix."""
    from services.registry import ModelRegistry

    from ._helpers import register_stub_provider, stub_resolution

    register_stub_provider(monkeypatch)
    roles: list[str] = []

    async def resolve_detailed(self, role, **kwargs):
        roles.append(role)
        text = "written by main" if role == "main" else "written by utility"
        return await stub_resolution(self, TestModel(custom_output_text=text, call_tools=[]))

    monkeypatch.setattr(ModelRegistry, "resolve_detailed", resolve_detailed)
    async with client_app() as (client, app):
        store = app.state.conversations
        cid = await store.create_conversation(OPERATOR_ID)
        store.record(cid, _turn("q0", "a0"))

        run = await _fold_now(client, app, cid)
        assert run.status is RunStatus.done, run.detail
        assert "main" in roles
        landed = next(
            e.body for e in run.stream.replay(0) if e.body.type == "conversation.compacted"
        )
        assert "written by main" in landed.summary
        assert "written by utility" not in landed.summary


# --- one fold, three triggers ------------------------------------------------


async def test_a_manual_fold_and_an_automatic_one_are_the_same_fold(monkeypatch):
    """**The guarantee the shared path exists for.** Two threads of identical shape, folded
    by the two different triggers, must come out indistinguishable apart from the reason.

    This is a regression test with a real history behind it. The operator's fold used to
    reach past ``agent.folding.fold`` and call the summarizer directly, assembling its own
    arguments as it went — so it emitted neither event, resolved a partial policy, and ran
    the summarizer against a different input budget (and later on a different model).
    Every one of those is invisible until something compares the two, which is what this
    does.

    Compared deliberately: the event sequence, what the fold measured, and what the
    checkpoint a reload reads says. Not the summary text — the same stub writes both, so
    asserting on it would pass whatever the two paths did."""
    async with client_app() as (client, app):
        patch_model_resolution(monkeypatch, output_text="what happened so far")
        store = app.state.conversations

        async def _thread() -> str:
            cid = await store.create_conversation(OPERATOR_ID)
            for i in range(4):
                store.record(cid, _turn(f"q{i}", f"a{i}"))
            return cid

        by_hand, by_threshold = await _thread(), await _thread()

        manual_run = await _fold_now(client, app, by_hand)
        assert manual_run.status is RunStatus.done, manual_run.detail

        # The same fold, reached through the turn-side trigger instead. `fold` is the one
        # entry point, so this is the automatic path's whole announcement layer.
        from agent.compaction_context import build_compaction_context
        from agent.folding import fold

        # A bare Run rather than a submitted one: `fold` only needs something to emit
        # onto, and a submitted run closes its stream the moment its orchestrator returns.
        run = Run(id="fold-auto", kind="compaction", owner_id=OPERATOR_ID, stream=RunStream())
        main = await app.state.models.resolve_detailed("main", owner_id=OPERATOR_ID)
        ctx = build_compaction_context(
            store=store,
            conversation_id=by_threshold,
            policy=build_auto_compact_policy(get_settings()),
            agent=build_agent(main.model, categories={}),
            deps=turn_deps(run, conversation_id=by_threshold),
            settings=get_settings(),
        )
        assert ctx is not None
        auto = await fold(run, ctx, reason="threshold")
        assert isinstance(auto, CompactionOutcome)

        manual = next(
            e.body for e in manual_run.stream.replay(0) if e.body.type == "conversation.compacted"
        )
        assert _fold_frames(manual_run) == _fold_frames(run)
        assert (manual.messages_compacted, manual.tokens_before, manual.tokens_after) == (
            auto.messages_compacted,
            auto.tokens_before,
            auto.tokens_after,
        )
        # The reason is the one thing that differs, and it differs everywhere it is
        # recorded — on the live event and on the checkpoint a reload reads back.
        assert (manual.reason, auto.reason) == ("manual", "threshold")
        reasons = []
        for cid in (by_hand, by_threshold):
            detail = (await client.get(f"/conversations/{cid}")).json()
            row = next(m for m in detail["messages"] if m["role"] == "compaction")
            reasons.append(row["compaction_reason"])
        assert reasons == ["manual", "threshold"]


async def test_the_fold_streams_the_summary_as_it_is_written(monkeypatch):
    """The pause is the whole problem this solves. `compaction.started` says a fold began
    and `conversation.compacted` says it finished; between them is a model call that runs
    for tens of seconds on a local endpoint, and until these frames existed there was
    nothing in it.

    What is asserted is the shape rather than the wording: deltas arrive *between* the two
    bracketing frames, and the text they carry adds up to what the model wrote. Not that it
    equals the stored summary — it deliberately does not, since the checkpoint is the
    stripped, anchored, fenced version and a delta is the raw working."""
    async with client_app() as (client, app):
        patch_model_resolution(monkeypatch, output_text="the story so far")
        store = app.state.conversations
        cid = await store.create_conversation(OPERATOR_ID)
        for i in range(4):
            store.record(cid, _turn(f"q{i}", f"a{i}"))

        run = await _fold_now(client, app, cid)
        assert run.status is RunStatus.done, run.detail

        frames = _fold_frames(run)
        assert frames[0] == "compaction.started"
        assert frames[-1] == "conversation.compacted"
        assert "compaction.delta" in frames

        deltas = [e.body for e in run.stream.replay(0) if e.body.type == "compaction.delta"]
        assert "".join(d.text for d in deltas) == "the story so far"


# --- a fold that does not land says so; one that does leaves real metrics -------------


async def test_a_fold_that_does_not_land_closes_the_compacting_section(monkeypatch):
    """`compaction.started` opens a live section on the client; with no checkpoint recorded
    nothing else ever closes it, and a reload would show a compaction the backend never
    had. The failure frame is what ends it, and carries the cause."""
    async with client_app() as (client, app):
        patch_model_resolution(monkeypatch, output_text="")
        store = app.state.conversations
        cid = await store.create_conversation(OPERATOR_ID)
        for i in range(4):
            store.record(cid, _turn(f"q{i}", f"a{i}"))

        run = await _fold_now(client, app, cid)
        frames = _fold_frames(run)
        assert frames[0] == "compaction.started"
        assert frames[-1] == "compaction.failed"
        failed = next(e.body for e in run.stream.replay(0) if e.body.type == "compaction.failed")
        assert failed.conversation_id == cid
        assert failed.reason == "manual"
        assert failed.message == run.detail
        # And a fold that landed nothing reports nothing: no empty metrics frame.
        assert "run.metrics" not in _emitted(run)


async def test_a_fold_that_raises_also_emits_compaction_failed(monkeypatch):
    from agent import folding

    async def boom(*_args, **_kwargs):
        raise RuntimeError("the summarizer fell over")

    monkeypatch.setattr(folding, "compact_conversation", boom)
    async with client_app() as (client, app):
        patch_model_resolution(monkeypatch)
        store = app.state.conversations
        cid = await store.create_conversation(OPERATOR_ID)
        store.record(cid, _turn("q0", "a0"))

        run = await _fold_now(client, app, cid)
        assert run.status is RunStatus.blocked
        failed = next(e.body for e in run.stream.replay(0) if e.body.type == "compaction.failed")
        assert failed.reason == "manual"
        assert "did not land" in failed.message


async def test_a_manual_fold_leaves_the_run_with_real_metrics_and_a_matching_reload(monkeypatch):
    """The registry closes every run with the metrics it stashed. A manual fold has no turn
    to stash any, so it used to close with an *empty* frame the client took as authoritative
    — the meter blanked. It now measures the folded thread, and the reload reads the same."""
    from services.registry import ModelRegistry

    async def window(self, owner_id):
        return 200_000

    monkeypatch.setattr(ModelRegistry, "main_context_window", window)
    async with client_app() as (client, app):
        patch_model_resolution(monkeypatch, output_text="a hand-made summary")
        store = app.state.conversations
        cid = await store.create_conversation(OPERATOR_ID)
        for i in range(4):
            store.record(cid, _turn(f"q{i}" * 400, f"a{i}" * 400))

        before = (await client.get(f"/conversations/{cid}")).json()["context"]["used"]
        run = await _fold_now(client, app, cid)
        assert run.status is RunStatus.done, run.detail

        assert run.metrics is not None
        assert run.context_window
        live = run.metrics.context_used
        assert live and live < before
        frames = [e.body for e in run.stream.replay(0) if e.body.type == "run.metrics"]
        assert len(frames) == 1 and frames[0].context_used == live
        assert frames[0].context_window == run.context_window

        detail = (await client.get(f"/conversations/{cid}")).json()
        assert detail["context"]["used"] == live


async def test_a_reload_measures_the_replay_not_the_transcript_on_the_estimate_path(monkeypatch):
    from core.config import get_settings
    from services.conversation_view import estimate_footprint
    from services.registry import ModelRegistry

    async def window(self, owner_id):
        return 200_000

    monkeypatch.setattr(ModelRegistry, "main_context_window", window)
    async with client_app() as (client, app):
        store = app.state.conversations
        cid = await store.create_conversation(OPERATOR_ID)
        for i in range(4):
            store.record(cid, _turn(f"q{i}" * 400, f"a{i}" * 400))
        full = (await client.get(f"/conversations/{cid}")).json()["context"]["used"]
        assert await _compact(store, cid) is not None

        replay = await store.replay_history(cid)
        expected = estimate_footprint(
            replay.messages,
            await store.get_overhead(cid),
            fallback_overhead_tokens=get_settings().context_overhead_fallback_tokens,
        )
        used = (await client.get(f"/conversations/{cid}")).json()["context"]["used"]
        assert used == expected < full


async def test_a_reload_after_a_fold_ignores_usage_reported_before_it(monkeypatch):
    """Every response before the fold reported its prompt against a history that no longer
    exists. With no reply since, the reload must fall back to the estimate — and the first
    reply after the fold is the first figure it may believe, as on the live stream."""
    from services.conversations import context_footprint
    from services.registry import ModelRegistry

    async def window(self, owner_id):
        return 200_000

    monkeypatch.setattr(ModelRegistry, "main_context_window", window)

    def reported(prompt: str, answer: str, tokens: int) -> list:
        return [
            ModelRequest(parts=[UserPromptPart(content=prompt)]),
            ModelResponse(
                parts=[TextPart(content=answer)],
                usage=RequestUsage(input_tokens=tokens, output_tokens=10),
            ),
        ]

    async with client_app() as (client, app):
        store = app.state.conversations
        cid = await store.create_conversation(OPERATOR_ID)
        for i in range(3):
            store.record(cid, reported(f"q{i}", f"a{i}", 90_000))
        assert (await client.get(f"/conversations/{cid}")).json()["context"]["used"] == 90_010

        assert await _compact(store, cid) is not None
        folded = (await client.get(f"/conversations/{cid}")).json()["context"]["used"]
        assert folded < 90_010  # the estimate over the summary, not the stale report

        fresh = reported("next", "reply", 4_000)
        store.record(cid, fresh)
        after = (await client.get(f"/conversations/{cid}")).json()["context"]["used"]
        assert after == context_footprint(fresh) == 4_010
