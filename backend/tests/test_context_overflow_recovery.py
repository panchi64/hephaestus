"""Surviving the context ceiling — the projected trigger, and the in-turn recovery.

Two related behaviours, one subject: a turn that would not fit.

The **trigger** now measures what this turn is about to cost, not what the last one did.
A thread at 70% with a 15% prompt in hand is a thread that must fold *now*; measuring the
history alone could only ever notice after the fact, which is why the threshold had to sit
at 95% to be safe.

The **recovery** is what happens when the provider refuses anyway — because the estimate
was low, because the operator pasted something enormous, because a tool returned far more
than anyone expected. The old behaviour stopped the turn *and left the oversized request in
history*, so it replayed on every later turn and the thread was finished. Now the turn
folds once and re-sends the very request that overran, against the summary.

The engine's own invariants are what these tests are really about: the persistence index
moves with the fold (or the turn re-records history it never wrote), the retry happens
once, and a correction — whose drop range is indexed into the pre-fold history — is barred
from folding at all.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import override

import pytest
from fastapi import FastAPI
from pydantic_ai import (
    DeferredToolRequests,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    RunContext,
)
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import TextPart, UserPromptPart
from pydantic_ai.models import Model, ModelRequestParameters, StreamedResponse
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.models.wrapper import WrapperModel
from pydantic_ai.settings import ModelSettings
from pydantic_ai.usage import RequestUsage

from agent import build_chat_orchestrator
from agent.compaction_context import CompactionContext
from agent.factory import build_agent
from agent.meta import Verdict
from agent.model_errors import (
    CONTEXT_OVERFLOW_AFTER_FOLD_DETAIL,
    CONTEXT_OVERFLOW_DETAIL,
    context_limit_message,
)
from agent.parking import ParkedTurn, park_for_input
from agent.summarize import AutoCompactPolicy, should_compact
from agent.turn import TurnResult, turn_deps
from agent.verify import verify_and_correct
from core.config import get_settings
from routes.deps import OPERATOR_ID
from runs import (
    CompactionStarted,
    ConversationCompacted,
    LimitNotice,
    Run,
    RunRegistry,
    RunStatus,
    RunStream,
    TurnOverhead,
)
from services.conversations import ConversationStore

from ._agent_helpers import make_stores
from ._helpers import app_state, bodies_of, client_app, fold_aware_model, is_fold_request

#: Measured, and empty. These fixtures run against a deliberately tiny window, and an
#: *unmeasured* overhead makes the trigger assume the shipped catalog — several thousand
#: tokens, which alone would overrun it and fold every thread on sight.
_NO_OVERHEAD = TurnOverhead(system=0, tools=0)

_POLICY = AutoCompactPolicy(enabled=True, threshold=0.80)


def _ctx_error() -> ModelHTTPError:
    """A provider refusing an over-long prompt, in the shape that answers on its own code
    rather than on a prose scan."""
    return ModelHTTPError(
        status_code=400,
        model_name="m",
        body={"error": {"code": "context_length_exceeded", "message": "too long"}},
    )


class _OverflowsThenAnswers(WrapperModel):
    """A model that refuses its first ``failures`` turn requests as over-long, then answers
    — and writes ``FOLDED AWAY`` when asked to fold, since the summary is the thread's own
    model continuing its conversation. ``requests`` counts the turn's requests only; a fold
    request overflows only when ``fold_overflows`` says so.

    The point of wrapping rather than faking: the retry has to survive the real library
    path — history cleaning, the resume-without-prompt shape, the streaming node — and a
    stub that answered without going through it would prove nothing about any of that."""

    def __init__(
        self, *, failures: int = 1, answer: str = "the answer", fold_overflows: bool = False
    ) -> None:
        super().__init__(fold_aware_model(answer=answer, summary="FOLDED AWAY"))
        self.failures: int = failures
        self.fold_overflows: bool = fold_overflows
        self.requests: int = 0
        self.folds: int = 0

    def _check(self, messages: list[ModelMessage]) -> None:
        if is_fold_request(messages):
            self.folds += 1
            if self.fold_overflows:
                raise _ctx_error()
            return
        self.requests += 1
        if self.requests <= self.failures:
            raise _ctx_error()

    @override
    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        self._check(messages)
        return await super().request(messages, model_settings, model_request_parameters)

    @override
    @asynccontextmanager
    async def request_stream(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
        run_context: RunContext[object] | None = None,
    ) -> AsyncGenerator[StreamedResponse]:
        self._check(messages)
        async with super().request_stream(
            messages, model_settings, model_request_parameters, run_context
        ) as stream:
            yield stream


def _turn(prompt: str, answer: str, input_tokens: int) -> list[ModelMessage]:
    """One recorded exchange whose answer reports a real prompt size, so the footprint is
    readable off it."""
    return [
        ModelRequest(parts=[UserPromptPart(content=prompt)]),
        ModelResponse(
            parts=[TextPart(content=answer)],
            usage=RequestUsage(input_tokens=input_tokens, output_tokens=10),
        ),
    ]


def _texts(messages: list[ModelMessage]) -> list[str]:
    out: list[str] = []
    for message in messages:
        for part in message.parts:
            content = getattr(part, "content", None)
            if isinstance(content, str):
                out.append(content)
    return out


async def _seed(store: ConversationStore, *, tail_tokens: int, turns: int = 4) -> str:
    cid = await store.create_conversation(OPERATOR_ID)
    for i in range(turns - 1):
        _ = store.record(cid, _turn(f"q{i}", f"a{i}", 100))
    _ = store.record(cid, _turn(f"q{turns - 1}", f"a{turns - 1}", tail_tokens))
    await store.set_overhead(cid, _NO_OVERHEAD)
    return cid


def _run_chat(app: FastAPI, cid: str | None, *, model: Model, prompt: str = "next question") -> Run:
    orch = build_chat_orchestrator(
        prompt,
        model=model,
        categories={},
        store=app_state(app, "conversations", ConversationStore) if cid else None,
        conversation_id=cid,
        context_window=10_000,
        auto_compact=_POLICY,
    )
    return app_state(app, "runs", RunRegistry).submit(
        kind="chat", owner_id=OPERATOR_ID, orchestrator=orch
    )


# --- the in-turn recovery ----------------------------------------------------


async def test_an_overflow_folds_the_thread_and_the_retried_request_answers():
    async with client_app() as (_client, app):
        store = app_state(app, "conversations", ConversationStore)
        cid = await _seed(store, tail_tokens=100)  # nowhere near the threshold
        before = len(await store.history(cid))

        run = _run_chat(app, cid, model=_OverflowsThenAnswers())
        await run.wait()

        assert run.status is RunStatus.done
        started = bodies_of(run, CompactionStarted)
        assert [s.reason for s in started] == ["overflow"]
        assert started[0].messages > 0
        compacted = bodies_of(run, ConversationCompacted)
        assert [c.reason for c in compacted] == ["overflow"]
        assert compacted[0].summary.endswith("FOLDED AWAY")

        # The persistence index moved with the fold: the tree gains the checkpoint and
        # exactly this turn's two messages — nothing re-recorded, nothing dropped. The
        # prompt went in first, so the checkpoint sits beneath it and the answer follows.
        await store.flush()
        store.evict_cache()
        after = _texts(await store.history(cid))
        assert len(after) == before + 3
        assert after[-3] == "next question"
        assert after[-2].endswith("FOLDED AWAY")
        assert after[-1] == "the answer"


async def test_the_retry_replays_the_folded_history_not_the_original():
    async with client_app() as (_client, app):
        store = app_state(app, "conversations", ConversationStore)
        cid = await _seed(store, tail_tokens=100)

        run = _run_chat(app, cid, model=_OverflowsThenAnswers())
        await run.wait()

        assert run.status is RunStatus.done
        replayed = _texts(await store.model_history(cid))
        # The summary now opens the model's view, and the turn that recovered sits on top.
        assert replayed[0].endswith("FOLDED AWAY")
        assert replayed[-2:] == ["next question", "the answer"]
        # And the folded turns are still in the transcript — nothing was destroyed.
        assert "q0" in _texts(await store.history(cid))


async def test_a_second_overflow_blocks_with_the_detail_the_client_keys_on():
    """One recovery per turn. A request that is still too big *after* a fold is too big
    for a reason folding can't reach, and re-folding would strip the thread for nothing."""
    async with client_app() as (_client, app):
        store = app_state(app, "conversations", ConversationStore)
        cid = await _seed(store, tail_tokens=100)

        model = _OverflowsThenAnswers(failures=2)
        run = _run_chat(app, cid, model=model)
        await run.wait()

        assert run.status is RunStatus.blocked
        # A *different* marker from the recoverable stop: the client keys "Compact and
        # retry" on that one, and folding again is exactly what just failed.
        assert run.detail == CONTEXT_OVERFLOW_AFTER_FOLD_DETAIL
        assert model.requests == 2  # tried once more, and only once
        assert len(bodies_of(run, CompactionStarted)) == 1
        notice = bodies_of(run, LimitNotice)[-1]
        assert notice.limit == "context"
        # The notice carries the same marker, so the toast can withhold the offer the
        # blocked turn withholds — the two must never disagree about the remedy.
        assert notice.detail == CONTEXT_OVERFLOW_AFTER_FOLD_DETAIL
        # Having already folded, the message must not send them round the same loop.
        assert "Compact now" not in notice.message
        assert "folded into a summary" in notice.message


async def test_the_summary_is_written_by_the_turns_own_model_on_its_recorded_turns():
    """The overflow fold summarizes the thread's *recorded* turns — this turn is not in the
    tree yet — on the model the turn runs on, so it is the smaller request of the two."""
    async with client_app() as (_client, app):
        store = app_state(app, "conversations", ConversationStore)
        cid = await _seed(store, tail_tokens=100)

        model = _OverflowsThenAnswers()
        run = _run_chat(app, cid, model=model)
        await run.wait()

        assert run.status is RunStatus.done
        assert model.folds == 1
        assert bodies_of(run, ConversationCompacted)[0].summary.endswith("FOLDED AWAY")


async def test_a_summary_request_that_overflows_too_stops_the_turn_with_the_notice():
    """No trimming fallback: if even the recorded turns do not fit, the fold fails like any
    other, and the turn stops with the ordinary context notice — which still offers the
    fold, since none landed."""
    async with client_app() as (_client, app):
        store = app_state(app, "conversations", ConversationStore)
        cid = await _seed(store, tail_tokens=100)

        model = _OverflowsThenAnswers(fold_overflows=True)
        run = _run_chat(app, cid, model=model)
        await run.wait()

        assert run.status is RunStatus.blocked
        assert run.detail == CONTEXT_OVERFLOW_DETAIL
        assert model.folds == 1
        assert not bodies_of(run, ConversationCompacted)


async def test_a_collapsed_fold_boundary_does_not_re_record_the_history():
    """The boundary between the folded history and the turn is *one* message.

    A fold keeps nothing verbatim, so the folded replay *is* the checkpoint — a request —
    and the turn's own prompt is another request right behind it, which both the library
    and our own normalization collapse into one. Slicing the turn out at a message index
    there hands the persist a message that is half checkpoint, half prompt, and the summary
    (with everything else the replay put in front of the prompt) is re-recorded as words
    the operator typed. A previous turn that stopped before it answered, folded along with
    the rest, must not come back either."""
    async with client_app() as (_client, app):
        store = app_state(app, "conversations", ConversationStore)
        cid = await _seed(store, tail_tokens=100)
        _ = store.record(
            cid,
            [ModelRequest(parts=[UserPromptPart(content="dangling question")])],
            blocked_reason=CONTEXT_OVERFLOW_DETAIL,
        )
        before = len(await store.history(cid))

        run = _run_chat(app, cid, model=_OverflowsThenAnswers())
        await run.wait()

        assert run.status is RunStatus.done
        await store.flush()
        store.evict_cache()
        after = _texts(await store.history(cid))
        # The prompt, the checkpoint, the answer — and nothing said twice. The prompt was
        # written down as the turn began, so the checkpoint lands beneath it and the answer
        # hangs off the checkpoint.
        assert len(after) == before + 3
        assert after[-3] == "next question"
        assert after[-1] == "the answer"
        assert sum("FOLDED AWAY" in text for text in after) == 1
        assert after.count("dangling question") == 1


async def test_the_gauge_moves_when_the_overflow_fold_lands_not_when_it_answers():
    """The prelude fold emits a fresh frame the moment it lands; the in-turn fold must too.
    A thread that folds and then overruns anyway would otherwise leave the operator looking
    at the pre-fold figure for good — a compaction that visibly did nothing."""
    async with client_app() as (_client, app):
        store = app_state(app, "conversations", ConversationStore)
        cid = await _seed(store, tail_tokens=7_000)

        run = _run_chat(app, cid, model=_OverflowsThenAnswers(failures=2))
        await run.wait()

        assert run.status is RunStatus.blocked
        events = run.stream.replay()
        fold = next(i for i, e in enumerate(events) if e.body.type == "conversation.compacted")
        after = [e.body for e in events[fold:] if e.body.type == "run.metrics"]
        assert after, "an in-turn fold must be followed by a fresh metrics frame"
        assert after[0].context_used is not None
        assert after[0].context_used < 7_000


async def test_a_stateless_turn_still_blocks_with_the_same_detail():
    """No store, no thread, nothing to fold — the honest outcome is the stop, and it must
    carry the same marker the client keys its offer on."""
    async with client_app() as (_client, app):
        run = _run_chat(app, None, model=_OverflowsThenAnswers())
        await run.wait()

        assert run.status is RunStatus.blocked
        assert run.detail == CONTEXT_OVERFLOW_DETAIL
        assert not bodies_of(run, CompactionStarted)
        notice = bodies_of(run, LimitNotice)[-1]
        assert notice.detail == CONTEXT_OVERFLOW_DETAIL
        assert "Compact now" in notice.message  # a fold is still the cheapest thing to try


async def test_compaction_switched_off_is_not_overruled_by_an_overflow():
    """Switching compaction off is an instruction, not a preference to be second-guessed:
    the thread stops and offers the fold, rather than performing it uninvited."""
    async with client_app() as (_client, app):
        store = app_state(app, "conversations", ConversationStore)
        cid = await _seed(store, tail_tokens=100)

        orch = build_chat_orchestrator(
            "next question",
            model=_OverflowsThenAnswers(),
            categories={},
            store=store,
            conversation_id=cid,
            context_window=10_000,
            auto_compact=AutoCompactPolicy(enabled=False, threshold=0.80),
        )
        run = app_state(app, "runs", RunRegistry).submit(
            kind="chat", owner_id=OPERATOR_ID, orchestrator=orch
        )
        await run.wait()

        assert run.status is RunStatus.blocked
        assert run.detail == CONTEXT_OVERFLOW_DETAIL
        assert not bodies_of(run, CompactionStarted)


async def test_a_thread_with_nothing_left_to_fold_blocks_rather_than_looping():
    async with client_app() as (_client, app):
        store = app_state(app, "conversations", ConversationStore)
        cid = await store.create_conversation(OPERATOR_ID)  # no turns at all
        await store.set_overhead(cid, _NO_OVERHEAD)

        run = _run_chat(app, cid, model=_OverflowsThenAnswers())
        await run.wait()

        assert run.status is RunStatus.blocked
        assert run.detail == CONTEXT_OVERFLOW_DETAIL
        assert not bodies_of(run, ConversationCompacted)


# --- the projected trigger ---------------------------------------------------


async def test_the_incoming_turn_counts_toward_the_threshold():
    """70% on the thread, ~15% arriving with the prompt: over the 80% share, though the
    history alone is well under it. Measuring only the history is what forced the old
    threshold up to 95% — the incoming turn had to fit in whatever was left over."""
    async with client_app() as (_client, app):
        store = app_state(app, "conversations", ConversationStore)
        cid = await _seed(store, tail_tokens=7_000)

        run = _run_chat(app, cid, model=TestModel(custom_output_text="ok"), prompt="x" * 6_000)
        await run.wait()

        assert run.status is RunStatus.done
        assert [c.reason for c in bodies_of(run, ConversationCompacted)] == ["threshold"]


async def test_the_same_thread_without_the_prompt_is_left_alone():
    async with client_app() as (_client, app):
        store = app_state(app, "conversations", ConversationStore)
        cid = await _seed(store, tail_tokens=7_000)

        run = _run_chat(app, cid, model=TestModel(custom_output_text="ok"), prompt="hi")
        await run.wait()

        assert run.status is RunStatus.done
        assert not bodies_of(run, ConversationCompacted)


async def test_the_gauge_moves_the_moment_the_fold_lands():
    """S2: every response left in the replay reported its size against the history that
    was just folded away. A frame emitted before the answer, reading the estimate rather
    than that stale figure, is what stops the gauge sitting at 70% through the very turn
    the fold made room for."""
    async with client_app() as (_client, app):
        store = app_state(app, "conversations", ConversationStore)
        cid = await _seed(store, tail_tokens=7_000)

        run = _run_chat(app, cid, model=TestModel(custom_output_text="ok"), prompt="x" * 6_000)
        await run.wait()

        events = run.stream.replay()
        fold = next(i for i, e in enumerate(events) if e.body.type == "conversation.compacted")
        after = [e.body for e in events[fold:] if e.body.type == "run.metrics"]
        assert after, "a fold must be followed by a fresh metrics frame"
        assert after[0].context_used is not None
        assert after[0].context_used < 7_000


async def test_a_thread_that_never_measured_its_overhead_does_not_assume_zero():
    """The brief and the tool schemas never reach the message history, so a thread whose
    turns predate the per-thread measurement has nothing to read them off. Zero is the one
    answer that is certainly wrong — the assembled catalog is worth thousands of tokens on
    every request, and assuming it away is how a fold arrives too late to help."""
    # A two-message thread that reports no usage at all: text-only it is worth nothing,
    # and the only thing that can push it over a 14k window is the assumed catalog.
    history = [*_turn("q", "a", 0)]
    assert should_compact(history, 14_000, 0.80, overhead=None)
    assert not should_compact(history, 14_000, 0.80, overhead=_NO_OVERHEAD)


async def test_a_parked_turn_carries_what_it_would_fold_with(tmp_path: Path):
    """An approval can sit for hours. The thread it resumes into is the one that was
    already near its ceiling, and nothing in the resume orchestrator could re-derive the
    policy, the store and the agent the summary is written on."""
    run = Run(id="p", kind="chat", owner_id=OPERATOR_ID, stream=RunStream())
    agent = build_agent(TestModel(), categories={})
    _agents, store = await make_stores(tmp_path)
    ctx = CompactionContext(
        store=store,
        conversation_id="c1",
        policy=_POLICY,
        agent=agent,
        deps=turn_deps(run, conversation_id="c1"),
        settings=get_settings(),
    )

    await park_for_input(run, agent, [], DeferredToolRequests(), set(), compaction=ctx)

    assert run.status is RunStatus.awaiting_input
    assert isinstance(run.parked_payload, ParkedTurn)
    assert run.parked_payload.compaction is ctx


# --- the verifier's size guard -----------------------------------------------


async def _reject(_prompt: str, _answer: str) -> Verdict:
    return Verdict(ok=False, reason="incomplete")


async def _no_model(_messages: list[ModelMessage], _info: AgentInfo) -> AsyncIterator[str]:
    for chunk in list[str]():
        yield chunk
    raise AttributeError("no model to re-drive with")


async def test_the_verifier_skips_a_correction_that_cannot_fit():
    """A correction is a second full pass over a history that has just finished its first,
    and it cannot fold (its drop range indexes the pre-fold history). Run against a window
    with no room left, it buys a context stop in place of a real answer."""
    run = Run(id="r", kind="chat", owner_id=OPERATOR_ID, stream=RunStream())
    run.context_window = 10_000
    run.context_overhead = _NO_OVERHEAD
    turn = TurnResult(
        answer="an answer",
        messages=[ModelRequest(parts=[UserPromptPart(content="x" * 40_000)])],
    )

    result = await verify_and_correct(
        run,
        build_agent(TestModel(), categories={}),
        "prompt",
        turn,
        set(),
        _reject,
        settings=get_settings(),
        context_threshold=0.80,
    )

    assert result is turn  # the answer survives
    notice = bodies_of(run, LimitNotice)[-1]
    assert notice.limit == "verify"
    assert notice.message == "skipped: no room for a re-attempt"


async def test_the_verifier_still_corrects_when_there_is_room():
    run = Run(id="r", kind="chat", owner_id=OPERATOR_ID, stream=RunStream())
    run.context_window = 10_000
    run.context_overhead = _NO_OVERHEAD
    turn = TurnResult(answer="an answer", messages=[])

    with pytest.raises(AttributeError):
        # An agent whose model raises on the re-drive — reaching that failure is the
        # assertion: the guard let the correction through rather than skipping it.
        _ = await verify_and_correct(
            run,
            build_agent(FunctionModel(stream_function=_no_model), categories={}),
            "prompt",
            turn,
            set(),
            _reject,
            settings=get_settings(),
            context_threshold=0.80,
        )
    messages = [b.message for b in bodies_of(run, LimitNotice)]
    assert messages == ["re-attempting: incomplete"]


# --- the operator-facing sentence --------------------------------------------


def test_the_stop_message_names_the_window_and_leads_with_what_to_do():
    run = Run(id="t", kind="chat", owner_id=OPERATOR_ID, stream=RunStream())
    run.context_window = 128_000
    fresh = context_limit_message(run)
    assert "128,000" in fresh
    assert "Compact now" in fresh
    # "Start a new chat" is the one option that abandons the thread; it must not lead.
    assert not fresh.startswith("Start a new chat")
    after_fold = context_limit_message(run, compacted=True)
    assert "Compact now" not in after_fold
    assert "128,000" in after_fold
