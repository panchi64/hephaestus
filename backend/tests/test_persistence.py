"""Persistence: the conversation store, write-behind, and chat continuity."""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator, AsyncIterator
from pathlib import Path
from typing import override

import pytest
from pydantic_ai import FunctionToolset, RunContext, ToolApproved
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    UserPromptPart,
)
from pydantic_ai.models import ModelRequestParameters, StreamedResponse
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.settings import ModelSettings
from sqlalchemy import Engine

import agent.engine as engine
from agent import ParkedTurn, build_chat_orchestrator, build_resume_orchestrator
from agent.meta import Verdict
from core.config import Settings
from core.db import init_db, make_engine
from core.vault import Vault
from routes.deps import app_state
from runs import Run, RunEnded, RunRegistry, RunStatus
from services.conversations import ConversationStore
from services.conversations.projection import project
from tools import RunDeps

from ._helpers import (
    client_app,
    collect_sse_events,
    register_stub_provider,
    stub_resolution,
)
from ._json import json_body, s


async def _unlocked_vault(tmp_path: Path, name: str = "keyfile.json") -> Vault:
    vault = Vault(tmp_path / name)
    if not vault.is_initialized:
        await vault.setup("pw")
    else:
        _ = await vault.unlock("pw")
    return vault


async def _fresh_store(tmp_path: Path) -> tuple[ConversationStore, Engine]:
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    return ConversationStore(engine, await _unlocked_vault(tmp_path)), engine


def _noop_toolset() -> FunctionToolset[RunDeps]:
    """One read-classed tool under the ``x`` category — a call the permission level lets
    through, so what stops the turn is the tool-call bound and nothing else."""
    toolset: FunctionToolset[RunDeps] = FunctionToolset()

    @toolset.tool_plain(metadata={"sensitivity": "read"})
    def noop(x: int) -> int:
        return x

    return toolset


def _bounds_tool_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    """Zero the tool-call ceiling, so the first call the model reaches for stops the turn."""
    monkeypatch.setattr(
        engine, "get_settings", lambda: Settings(agent_request_limit=25, agent_tool_calls_limit=0)
    )


def _calls_noop() -> FunctionModel:
    """A model that reaches for ``x_noop`` on every request, whatever the history holds.

    `TestModel(call_tools=…)` calls its tools only while the history carries no response
    at all, so on the *second* stopped turn of a thread it answers in text instead and the
    bound is never tripped. These tests are about what a stop leaves behind across several
    turns, so the model has to be the one thing that does not change between them.

    Both halves are supplied because the chat orchestrator streams every request, and a
    `FunctionModel` with no `stream_function` asserts rather than answering.
    """
    args = {"x": 1}

    async def respond(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[ToolCallPart("x_noop", args)])

    async def stream(
        _messages: list[ModelMessage], _info: AgentInfo
    ) -> AsyncIterator[dict[int, DeltaToolCall]]:
        yield {0: DeltaToolCall(name="x_noop", json_args=json.dumps(args))}

    return FunctionModel(respond, stream_function=stream)


async def test_cache_is_bounded_and_evicted_trees_rehydrate(tmp_path: Path):
    # Every cached entry holds a fully decrypted tree (inline image bytes included), so
    # an unbounded cache grows to every conversation the process ever touched. Eviction
    # is safe precisely because a miss rehydrates.
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    store = ConversationStore(engine, await _unlocked_vault(tmp_path), max_cached_conversations=2)
    await store.start()

    convs = [await store.create_conversation("operator", title=f"t{i}") for i in range(5)]
    assert len(store.cached_ids) <= 2

    # The evicted ones are gone from memory but still readable.
    for conv in convs:
        assert await store.history(conv) == []
    assert len(store.cached_ids) <= 2
    await store.stop()


async def test_a_conversation_with_queued_writes_is_never_evicted(tmp_path: Path):
    # `record()` extends the cached tree in place and queues only the new slice, so
    # evicting between the two would leave the next append building on an empty tree.
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    vault = await _unlocked_vault(tmp_path)
    store = ConversationStore(engine, vault, max_cached_conversations=1)
    # Never started, so nothing drains: every submitted job stays pending.
    conv = await store.create_conversation("operator", title="held")
    _ = store.record(conv, [ModelRequest(parts=[UserPromptPart("hello")])])

    for i in range(5):
        _ = await store.create_conversation("operator", title=f"other{i}")

    assert conv in store.cached_ids
    cached = store.cache_get(conv)
    assert cached is not None
    assert len(cached.nodes) == 1


async def test_store_records_and_rehydrates_from_db(tmp_path: Path):
    store, engine = await _fresh_store(tmp_path)
    await store.start()
    conv = await store.create_conversation("operator", title="t")

    reg = RunRegistry()

    async def run_turn(text: str):
        orch = build_chat_orchestrator(
            text,
            model=TestModel(custom_output_text=f"re:{text}"),
            categories={},  # no tools → 2 messages per turn
            store=store,
            conversation_id=conv,
        )
        run = reg.submit(kind="chat", owner_id="operator", orchestrator=orch)
        await run.wait()

    await run_turn("hello")
    await run_turn("again")

    # The live working set reflects both turns immediately (no DB read).
    assert len(await store.history(conv)) == 4

    # A cold store rehydrates the same history from the durable record (it must
    # unlock the same keyfile to decrypt).
    await store.stop()
    cold = ConversationStore(engine, await _unlocked_vault(tmp_path))
    await cold.start()
    rehydrated = await cold.history(conv)
    assert [m.kind for m in rehydrated] == ["request", "response", "request", "response"]
    await cold.stop()


async def test_context_footprint_reads_last_response(tmp_path: Path):
    """The footprint helper reconstructs the active path's most recent response
    usage — what seeds the context meter when an existing conversation loads, and
    what the live run reports. It is the LAST response's prompt+generation, never a
    sum across turns."""
    from pydantic_ai import ModelResponse

    from services.conversations import context_footprint

    store, _ = await _fresh_store(tmp_path)
    await store.start()
    conv = await store.create_conversation("operator", title="t")

    # No turns yet → nothing to report.
    assert context_footprint(await store.history(conv)) is None

    reg = RunRegistry()

    async def run_turn(text: str):
        orch = build_chat_orchestrator(
            text,
            model=TestModel(custom_output_text=f"re:{text}"),
            categories={},
            store=store,
            conversation_id=conv,
        )
        run = reg.submit(kind="chat", owner_id="operator", orchestrator=orch)
        await run.wait()

    await run_turn("hello")
    await run_turn("again")

    history = await store.history(conv)
    last_response = next(m for m in reversed(history) if isinstance(m, ModelResponse))
    expected = last_response.usage.input_tokens + last_response.usage.output_tokens
    assert context_footprint(history) == expected
    await store.stop()


async def test_content_is_encrypted_at_rest(tmp_path: Path):
    from sqlmodel import Session, select

    from models.conversation import Message

    store, engine = await _fresh_store(tmp_path)
    await store.start()
    conv = await store.create_conversation("operator")

    reg = RunRegistry()
    orch = build_chat_orchestrator(
        "tell me the SECRET-TOKEN-XYZ",
        model=TestModel(custom_output_text="the answer is SECRET-TOKEN-XYZ"),
        categories={},
        store=store,
        conversation_id=conv,
    )
    run = reg.submit(kind="chat", owner_id="operator", orchestrator=orch)
    await run.wait()
    await store.stop()  # flush the write-behind queue

    # Raw rows on disk must not contain the plaintext.
    with Session(engine) as session:
        rows = session.exec(select(Message).where(Message.conversation_id == conv)).all()
    assert rows
    for row in rows:
        assert "SECRET-TOKEN-XYZ" not in row.blob
        assert "SECRET-TOKEN-XYZ" not in row.text


async def test_blocked_turn_persists_with_its_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # A tool-calls bound tripped after the model already committed to a call: the
    # turn stops blocked, but what ran (the tool-call response) is real conversation
    # content — persist it, tagged with why it stopped, so a reload shows the same
    # marker the live stream rendered rather than a turn that silently never happened.
    toolset = _noop_toolset()
    _bounds_tool_calls(monkeypatch)
    store, db_engine = await _fresh_store(tmp_path)
    await store.start()
    conv = await store.create_conversation("operator", title="t")

    reg = RunRegistry()
    orch = build_chat_orchestrator(
        "call the tool",
        model=_calls_noop(),
        categories={"x": toolset},
        store=store,
        conversation_id=conv,
    )
    run = reg.submit(kind="chat", owner_id="operator", orchestrator=orch)
    await run.wait()

    assert run.status is RunStatus.blocked
    views = await store.messages_view(conv)
    assert views[0].role == "user"
    assert views[-1].role == "assistant"
    # The marker names the bound that tripped — one of this run's own per-turn budgets.
    # A bare "usage limit reached" would read as a provider rate limit and send the
    # operator looking for an account quota that has nothing to do with the stop.
    reason = "this run hit its tool-call limit for a single turn and stopped"
    assert views[-1].blocked_reason == reason

    # Reload parity: a cold store rehydrates the same marker from the DB.
    await store.stop()
    cold = ConversationStore(db_engine, await _unlocked_vault(tmp_path))
    await cold.start()
    cold_views = await cold.messages_view(conv)
    assert cold_views[-1].blocked_reason == reason
    await cold.stop()


async def test_wall_clock_timeout_persists_the_partial_turn(tmp_path: Path):
    # The registry force-cancels the orchestrator's task on a wall-clock timeout,
    # interrupting it before its own normal finalize path can run — the engine's
    # `on_timeout` hook is what persists the partial turn instead, so a reload
    # doesn't silently drop it (see `RunRegistry._flush_timeout`).
    import asyncio

    toolset: FunctionToolset[RunDeps] = FunctionToolset()
    hang = asyncio.Event()

    @toolset.tool_plain(metadata={"sensitivity": "read"})
    async def slow(x: int) -> int:
        _ = await asyncio.wait_for(hang.wait(), timeout=2.0)
        return x

    store, db_engine = await _fresh_store(tmp_path)
    await store.start()
    conv = await store.create_conversation("operator", title="t")

    reg = RunRegistry(wall_clock_timeout_s=0.05, inactivity_timeout_s=None)
    orch = build_chat_orchestrator(
        "call the tool",
        model=TestModel(call_tools=["x_slow"]),
        categories={"x": toolset},
        store=store,
        conversation_id=conv,
    )
    run = reg.submit(kind="chat", owner_id="operator", orchestrator=orch)
    await run.wait()

    assert run.status is RunStatus.blocked
    views = await store.messages_view(conv)
    assert views[0].role == "user"
    assert views[-1].blocked_reason == "this run hit the 1-second overall limit"

    # Reload parity: a cold store rehydrates the same marker from the DB.
    await store.stop()
    cold = ConversationStore(db_engine, await _unlocked_vault(tmp_path))
    await cold.start()
    cold_views = await cold.messages_view(conv)
    assert cold_views[-1].blocked_reason == "this run hit the 1-second overall limit"
    await cold.stop()


async def test_inactivity_timeout_in_setup_window_persists_the_prompt(tmp_path: Path):
    # The inactivity clock starts at run start and is refreshed only by events. If it
    # trips in the pre-model window — the model never produced a first token — the turn
    # has no response, only the operator's own request. Without the fix, the stop would
    # persist nothing (the partial history is still empty) and the operator's message
    # would vanish on reload; the engine's timeout hook now persists the turn (falling
    # back to the typed prompt), and `record()` stamps the marker on the request node
    # since there's no response to carry it.
    import asyncio
    from contextlib import asynccontextmanager

    from pydantic_ai.models.test import TestModel

    class _SilentModel(TestModel):
        # Never yields a first token — the request stream hangs, so the inactivity
        # watchdog trips while the model is still silent.
        @asynccontextmanager
        @override
        async def request_stream(
            self,
            messages: list[ModelMessage],
            model_settings: ModelSettings | None,
            model_request_parameters: ModelRequestParameters,
            run_context: RunContext[object] | None = None,
        ) -> AsyncGenerator[StreamedResponse]:
            _ = await asyncio.Event().wait()
            async with super().request_stream(
                messages, model_settings, model_request_parameters, run_context
            ) as stream:  # pragma: no cover
                yield stream  # pragma: no cover

    store, db_engine = await _fresh_store(tmp_path)
    await store.start()
    conv = await store.create_conversation("operator", title="t")

    reg = RunRegistry(wall_clock_timeout_s=5.0, inactivity_timeout_s=0.05)
    orch = build_chat_orchestrator(
        "hello there",
        model=_SilentModel(),
        categories={},
        store=store,
        conversation_id=conv,
    )
    run = reg.submit(kind="chat", owner_id="operator", orchestrator=orch)
    await run.wait()

    assert run.status is RunStatus.blocked
    views = await store.messages_view(conv)
    # Only the operator's own message survives — the model never answered.
    assert len(views) == 1
    assert views[0].role == "user"
    assert views[0].content == "hello there"
    # The marker lands on the request node (there's no response to carry it).
    assert views[0].blocked_reason == "no activity for 1 second"

    # Reload parity: a cold store rehydrates the same marker from the DB.
    await store.stop()
    cold = ConversationStore(db_engine, await _unlocked_vault(tmp_path))
    await cold.start()
    cold_views = await cold.messages_view(conv)
    assert cold_views[0].blocked_reason == "no activity for 1 second"
    await cold.stop()


async def test_inactivity_timeout_during_the_orchestrator_prelude_persists_the_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # Everything before the first model call — the history read, auto-compaction, the
    # attachment and prompt-context resolution — awaits and emits nothing, so the
    # inactivity watchdog is ticking against a run that looks idle. Compaction's own
    # bound and the inactivity bound even share a default, so a compaction that runs to
    # its limit trips the watchdog. The flush hooks must therefore be armed *before* that
    # window, not after it: armed late they are `None` exactly when they are needed and
    # the operator's typed message vanishes on reload.
    import asyncio

    from pydantic_ai.messages import ModelRequest, UserPromptPart

    store, _db_engine = await _fresh_store(tmp_path)
    await store.start()
    conv = await store.create_conversation("operator", title="t")
    # Seed a turn so the orchestrator actually takes the history-reading path.
    _ = store.record(conv, [ModelRequest(parts=[UserPromptPart("earlier")])])

    async def _hangs(_conversation_id: str) -> None:  # stands in for a slow compaction/history read
        _ = await asyncio.Event().wait()

    monkeypatch.setattr(store, "model_history", _hangs)

    reg = RunRegistry(wall_clock_timeout_s=5.0, inactivity_timeout_s=0.05)
    orch = build_chat_orchestrator(
        "hello there",
        model=TestModel(),
        categories={},
        store=store,
        conversation_id=conv,
    )
    run = reg.submit(kind="chat", owner_id="operator", orchestrator=orch)
    await run.wait()

    assert run.status is RunStatus.blocked
    views = await store.messages_view(conv)
    assert [v.content for v in views] == ["earlier", "hello there"]
    assert views[-1].blocked_reason == "no activity for 1 second"

    await store.stop()


async def test_cancel_persists_the_partial_turn(tmp_path: Path):
    # A manual Stop (`RunRegistry.cancel`) force-cancels the orchestrator's task just
    # like a wall-clock timeout does — the engine's `on_cancel` hook (the cancel
    # counterpart of `on_timeout`) is what persists the partial turn instead, so a
    # reload doesn't silently drop it (and the run's own terminal status/outcome, set
    # by the registry's own cancellation handling, stay `cancelled` — the hook must not
    # clobber them).
    import asyncio

    toolset: FunctionToolset[RunDeps] = FunctionToolset()
    started = asyncio.Event()
    hang = asyncio.Event()

    @toolset.tool_plain(metadata={"sensitivity": "read"})
    async def slow(x: int) -> int:
        started.set()
        _ = await hang.wait()  # never set — cancelled before it would return
        return x

    store, db_engine = await _fresh_store(tmp_path)
    await store.start()
    conv = await store.create_conversation("operator", title="t")

    reg = RunRegistry()
    orch = build_chat_orchestrator(
        "call the tool",
        model=TestModel(call_tools=["x_slow"]),
        categories={"x": toolset},
        store=store,
        conversation_id=conv,
    )
    run = reg.submit(kind="chat", owner_id="operator", orchestrator=orch)
    _ = await started.wait()  # let it commit to the tool call and start hanging inside it
    assert await reg.cancel(run.id) is True
    await run.wait()

    assert run.status is RunStatus.cancelled
    ended = run.stream.replay()[-1].body
    assert isinstance(ended, RunEnded) and ended.outcome == "cancelled"
    views = await store.messages_view(conv)
    assert views[0].role == "user"
    assert views[-1].blocked_reason == "cancelled by the operator"

    # Reload parity: a cold store rehydrates the same marker from the DB.
    await store.stop()
    cold = ConversationStore(db_engine, await _unlocked_vault(tmp_path))
    await cold.start()
    cold_views = await cold.messages_view(conv)
    assert cold_views[-1].blocked_reason == "cancelled by the operator"
    await cold.stop()


async def test_double_cancel_does_not_duplicate_the_persisted_turn(tmp_path: Path):
    # A repeated cancel() on the same run before the first cancellation actually
    # lands (no other await in between — mirrors a double-click Stop, or a client
    # retry against POST /runs/{id}/cancel, which has no server-side dedup) must be
    # a no-op the second time: `run.status` stays `running` until the task's
    # CancelledError is delivered on a later event-loop tick, so a naive re-entry
    # would re-flush and double-record the same partial turn.
    import asyncio

    toolset: FunctionToolset[RunDeps] = FunctionToolset()
    started = asyncio.Event()
    hang = asyncio.Event()

    @toolset.tool_plain(metadata={"sensitivity": "read"})
    async def slow(x: int) -> int:
        started.set()
        _ = await hang.wait()  # never set — cancelled before it would return
        return x

    store, _db_engine = await _fresh_store(tmp_path)
    await store.start()
    conv = await store.create_conversation("operator", title="t")

    reg = RunRegistry()
    orch = build_chat_orchestrator(
        "call the tool",
        model=TestModel(call_tools=["x_slow"]),
        categories={"x": toolset},
        store=store,
        conversation_id=conv,
    )
    run = reg.submit(kind="chat", owner_id="operator", orchestrator=orch)
    _ = await started.wait()  # let it commit to the tool call and start hanging inside it

    # Two cancels back-to-back, no other await between them — the task is still
    # `running` (its CancelledError hasn't landed yet) for the second call.
    assert await reg.cancel(run.id) is True
    assert run.status is RunStatus.running
    assert await reg.cancel(run.id) is True

    await run.wait()

    assert run.status is RunStatus.cancelled
    views = await store.messages_view(conv)
    # Exactly one user prompt + one partial assistant turn — not duplicated.
    assert [v.role for v in views] == ["user", "assistant"]
    assert views[-1].blocked_reason == "cancelled by the operator"

    await store.stop()


async def test_cancel_parked_run_persists_the_parked_turn(tmp_path: Path):
    # Cancelling a *parked* run (RunRegistry.cancel's awaiting_input branch) has no
    # task left to interrupt — a parked turn's persistence is otherwise only ever
    # recorded on resume (`agent.finalize.finalize`'s parked branch just wires resume
    # context). The engine's `on_park_cancel` hook — the parked counterpart of
    # `on_cancel` — is what persists it instead, so cancelling instead of resuming
    # doesn't silently drop the operator's own prompt (backend-correctness-01).
    toolset: FunctionToolset[RunDeps] = FunctionToolset()

    @toolset.tool_plain(requires_approval=True)
    def delete_thing(name: str) -> str:
        return f"deleted {name}"

    store, db_engine = await _fresh_store(tmp_path)
    await store.start()
    conv = await store.create_conversation("operator", title="t")

    reg = RunRegistry()
    orch = build_chat_orchestrator(
        "delete the thing",
        model=TestModel(custom_output_text="done"),
        categories={"danger": toolset},
        store=store,
        conversation_id=conv,
    )
    run = reg.submit(kind="chat", owner_id="operator", orchestrator=orch, conversation_id=conv)
    await run.wait()
    assert run.status is RunStatus.awaiting_input

    assert await reg.cancel(run.id) is True
    assert run.status is RunStatus.cancelled
    assert run.stream.closed

    views = await store.messages_view(conv)
    assert views[0].role == "user"
    assert views[-1].blocked_reason == "cancelled by the operator"

    # Reload parity: a cold store rehydrates the same marker from the DB.
    await store.stop()
    cold = ConversationStore(db_engine, await _unlocked_vault(tmp_path))
    await cold.start()
    cold_views = await cold.messages_view(conv)
    assert cold_views[-1].blocked_reason == "cancelled by the operator"
    await cold.stop()


async def test_unhandled_exception_persists_the_partial_turn_and_errors(tmp_path: Path):
    # Anything that escapes `drive_turn` besides its specific bound catches (here: a
    # tool raising a plain exception) must not silently drop the operator's own prompt
    # (and whatever the turn had already produced) from persistence — the orchestrator's
    # broad `except Exception` flushes it, carrying a legible marker, before the
    # registry's own generic handler records the run as `error`.
    toolset: FunctionToolset[RunDeps] = FunctionToolset()

    @toolset.tool_plain(metadata={"sensitivity": "read"})
    def boom() -> int:
        raise RuntimeError("tool exploded")

    store, db_engine = await _fresh_store(tmp_path)
    await store.start()
    conv = await store.create_conversation("operator", title="t")

    reg = RunRegistry()
    orch = build_chat_orchestrator(
        "call the tool",
        model=TestModel(call_tools=["x_boom"]),
        categories={"x": toolset},
        store=store,
        conversation_id=conv,
    )
    run = reg.submit(kind="chat", owner_id="operator", orchestrator=orch)
    await run.wait()

    assert run.status is RunStatus.error
    err = run.stream.replay()[-1].body
    assert err.type == "run.error"

    views = await store.messages_view(conv)
    assert views[0].role == "user"
    assert views[-1].blocked_reason == "an unexpected error stopped this turn"

    # Reload parity: a cold store rehydrates the same marker from the DB.
    await store.stop()
    cold = ConversationStore(db_engine, await _unlocked_vault(tmp_path))
    await cold.start()
    cold_views = await cold.messages_view(conv)
    assert cold_views[-1].blocked_reason == "an unexpected error stopped this turn"
    await cold.stop()


async def test_cooperative_cancel_flag_stops_the_turn(tmp_path: Path):
    # `cancel_requested`, flipped directly here with no `RunRegistry.cancel()` and no
    # `task.cancel()` anywhere in the picture, must still stop a running turn at its
    # next step boundary (`agent/turn.py`'s `report_progress`) — proving the flag is now a
    # real, independently-effective cooperative-cancel signal rather than dead state.
    toolset: FunctionToolset[RunDeps] = FunctionToolset()
    run_ref: list[Run | None] = [None]

    @toolset.tool_plain(metadata={"sensitivity": "read"})
    def flag(x: int) -> int:
        current = run_ref[0]
        assert current is not None
        current.cancel_requested = True
        return x

    store, _ = await _fresh_store(tmp_path)
    await store.start()
    conv = await store.create_conversation("operator", title="t")

    reg = RunRegistry()
    orch = build_chat_orchestrator(
        "call the tool",
        model=TestModel(call_tools=["x_flag"]),
        categories={"x": toolset},
        store=store,
        conversation_id=conv,
    )
    run = reg.submit(kind="chat", owner_id="operator", orchestrator=orch)
    run_ref[0] = run
    await run.wait()

    assert run.status is RunStatus.cancelled
    await store.stop()


async def test_foreign_keys_are_enforced():
    # The Message → Conversation FK is only real if SQLite's pragma is on; an
    # orphan insert must fail loudly rather than silently land.
    from sqlalchemy.exc import IntegrityError
    from sqlmodel import Session

    from models.conversation import Message

    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    raised = False
    with Session(engine) as session:
        session.add(
            Message(
                conversation_id="no-such-conv",
                seq=0,
                kind="response",
                text="x",
                blob="y",
                attachment_ids=[],
            )
        )
        try:
            session.commit()
        except IntegrityError:
            raised = True
    assert raised


async def test_second_turn_continues_prior_history(tmp_path: Path):
    store, _ = await _fresh_store(tmp_path)
    await store.start()
    conv = await store.create_conversation("operator")

    reg = RunRegistry()
    seen_history_lengths: list[int] = []

    # A model that records how much history it was given each turn.
    def make_model(reply: str) -> TestModel:
        return TestModel(custom_output_text=reply)

    orch1 = build_chat_orchestrator(
        "first", model=make_model("a"), categories={}, store=store, conversation_id=conv
    )
    run1 = reg.submit(kind="chat", owner_id="operator", orchestrator=orch1)
    await run1.wait()
    seen_history_lengths.append(len(await store.history(conv)))

    orch2 = build_chat_orchestrator(
        "second", model=make_model("b"), categories={}, store=store, conversation_id=conv
    )
    run2 = reg.submit(kind="chat", owner_id="operator", orchestrator=orch2)
    await run2.wait()
    seen_history_lengths.append(len(await store.history(conv)))

    assert seen_history_lengths == [2, 4]  # history grows across turns
    await store.stop()


async def test_verifier_correction_persists_clean_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # A judge-rejected answer + the synthetic nudge must NOT end up in history —
    # only the original request → corrected answer.
    monkeypatch.setattr(
        engine, "get_settings", lambda: Settings(verify_enabled=True, verify_heuristic=False)
    )
    store, _ = await _fresh_store(tmp_path)
    await store.start()
    conv = await store.create_conversation("operator")

    verdicts = [Verdict(ok=False, reason="add more detail")]

    async def judge(_request: str, _answer: str) -> Verdict:
        return verdicts.pop(0) if verdicts else Verdict(ok=True)

    reg = RunRegistry()
    orch = engine.build_chat_orchestrator(
        "summarize it",
        model=TestModel(custom_output_text="the summary"),
        categories={},  # no tools → a clean 2-message turn
        judge=judge,
        store=store,
        conversation_id=conv,
    )
    run = reg.submit(kind="chat", owner_id="operator", orchestrator=orch)
    await run.wait()
    assert run.status is RunStatus.done

    history = await store.history(conv)
    assert [m.kind for m in history] == ["request", "response"]  # not the 4-msg transcript
    texts = [project(m)[1] for m in history]
    assert not any("did not fully satisfy" in t for t in texts)  # nudge didn't leak
    await store.stop()


def _danger_categories() -> dict[str, FunctionToolset[RunDeps]]:
    toolset: FunctionToolset[RunDeps] = FunctionToolset()

    @toolset.tool_plain(requires_approval=True)
    def delete_thing(name: str) -> str:
        return f"deleted {name}"

    return {"danger": toolset}


async def test_verify_park_persists_once_on_resume(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # The verifier's corrective re-attempt itself parks for approval. Nothing is
    # persisted while parked; the resume persists exactly once.
    monkeypatch.setattr(
        engine, "get_settings", lambda: Settings(verify_enabled=True, verify_heuristic=False)
    )
    store, _ = await _fresh_store(tmp_path)
    await store.start()
    conv = await store.create_conversation("operator")

    async def judge(_request: str, _answer: str) -> Verdict:
        return Verdict(ok=False, reason="redo it")  # always reject → trigger a correction

    def _is_correction(messages: list[ModelMessage]) -> bool:
        text = " ".join(
            part.content
            for message in messages
            for part in message.parts
            if isinstance(part, UserPromptPart | TextPart | SystemPromptPart | ThinkingPart)
            and isinstance(part.content, str)
        )
        return "did not fully satisfy" in text

    def _tool_already_ran(messages: list[ModelMessage]) -> bool:
        return any(
            type(part).__name__ == "ToolReturnPart"
            for message in messages
            for part in message.parts
        )

    async def stream_fn(
        messages: list[ModelMessage], info: AgentInfo
    ) -> AsyncIterator[str | dict[int, DeltaToolCall]]:
        # First attempt: a plain text answer (no tool). The corrective re-attempt
        # calls the sensitive tool, which parks the run for approval. After the
        # approved tool runs, finish with text.
        if _tool_already_ran(messages):
            yield "all done"
        elif _is_correction(messages):
            tool_name = info.function_tools[0].name
            yield {0: DeltaToolCall(name=tool_name, json_args='{"name": "x"}')}
        else:
            yield "first answer"

    reg = RunRegistry()
    orch = engine.build_chat_orchestrator(
        "do the thing",
        model=FunctionModel(stream_function=stream_fn),
        categories=_danger_categories(),
        judge=judge,
        store=store,
        conversation_id=conv,
    )
    run = reg.submit(kind="chat", owner_id="operator", orchestrator=orch)
    await run.wait()

    # Parked by the correction — persistence context wired, and only the operator's
    # prompt (written down as the turn began) on record.
    assert run.status is RunStatus.awaiting_input
    assert [project(m)[1] for m in await store.history(conv)] == ["do the thing"]
    parked = run.parked_payload
    assert isinstance(parked, ParkedTurn)
    assert parked.conversation_id == conv
    assert parked.persist_from == 0
    assert parked.prompt_message_id == run.prompt_message_id is not None

    call_id = parked.requests.approvals[0].tool_call_id
    _ = await reg.resume(
        run.id, build_resume_orchestrator(parked, {call_id: ToolApproved()}, store=store)
    )
    await run.wait()
    assert run.status is RunStatus.done

    # Persisted exactly once on resume — and cleaned: the rejected "first answer"
    # and the synthetic nudge are dropped, the approved final answer is kept.
    history = await store.history(conv)
    texts = [project(m)[1] for m in history]
    assert any("all done" in t for t in texts)
    assert not any("first answer" in t for t in texts)
    assert not any("did not fully satisfy" in t for t in texts)
    assert texts.count("do the thing") == 1  # the prompt is not recorded a second time
    await store.stop()


async def test_chat_route_returns_conversation_and_continues(monkeypatch: pytest.MonkeyPatch):
    from services.registry import ModelRegistry, ResolvedModel

    async def fake_resolve_detailed(
        self: ModelRegistry, _role: str, **_kwargs: object
    ) -> ResolvedModel:
        # call_tools=[] → a plain text turn; the default catalog's approval-gated
        # tool would otherwise park the run and stall the SSE this test reads.
        return await stub_resolution(self, TestModel(custom_output_text="hi", call_tools=[]))

    register_stub_provider(monkeypatch)
    monkeypatch.setattr(ModelRegistry, "resolve_detailed", fake_resolve_detailed)

    async with client_app() as (client, app):
        first = await client.post("/chat", json={"prompt": "hello"})
        assert first.status_code == 202
        conv_id = s(json_body(first)["conversation_id"])
        run_id = s(json_body(first)["run_id"])
        _ = await collect_sse_events(client, run_id)

        # continue the same conversation
        second = await client.post("/chat", json={"prompt": "again", "conversation_id": conv_id})
        assert json_body(second)["conversation_id"] == conv_id
        _ = await collect_sse_events(client, s(json_body(second)["run_id"]))

        history = await app_state(app).conversations.history(conv_id)

    assert len(history) >= 4  # two turns, both persisted to the same conversation


async def test_continuing_a_stopped_turn_retires_its_marker_for_good(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # The stop marker is a standing "this turn didn't finish — resume it?" prompt.
    # Once the operator has resumed it, leaving the warning up is the UI arguing with
    # itself, so the clear has to be as durable as the marker was.
    toolset = _noop_toolset()
    _bounds_tool_calls(monkeypatch)
    store, db_engine = await _fresh_store(tmp_path)
    await store.start()
    conv = await store.create_conversation("operator", title="t")

    reg = RunRegistry()
    orch = build_chat_orchestrator(
        "call the tool",
        model=_calls_noop(),
        categories={"x": toolset},
        store=store,
        conversation_id=conv,
    )
    run = reg.submit(kind="chat", owner_id="operator", orchestrator=orch)
    await run.wait()

    views = await store.messages_view(conv)
    blocked = next(v for v in views if v.blocked_reason is not None)

    assert await store.clear_blocked_reason(conv, blocked.id) is True
    assert all(v.blocked_reason is None for v in await store.messages_view(conv))
    # Already-clear is not an error, but reports that there was nothing to retire.
    assert await store.clear_blocked_reason(conv, blocked.id) is False

    # Reload parity: the retired marker must not come back from the DB.
    await store.stop()
    cold = ConversationStore(db_engine, await _unlocked_vault(tmp_path))
    await cold.start()
    assert all(v.blocked_reason is None for v in await cold.messages_view(conv))
    await cold.stop()


async def test_clearing_an_unknown_id_retires_the_last_stop_on_the_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # A turn that stopped before anything persisted is shown under the client's own
    # optimistic id, which is not a node id here. The marker it means is still the
    # only one on the path, so the fallback retires that rather than no-oping and
    # leaving a warning the operator has visibly resolved.
    toolset = _noop_toolset()
    _bounds_tool_calls(monkeypatch)
    store, _ = await _fresh_store(tmp_path)
    await store.start()
    conv = await store.create_conversation("operator", title="t")

    reg = RunRegistry()
    orch = build_chat_orchestrator(
        "call the tool",
        model=_calls_noop(),
        categories={"x": toolset},
        store=store,
        conversation_id=conv,
    )
    run = reg.submit(kind="chat", owner_id="operator", orchestrator=orch)
    await run.wait()
    assert any(v.blocked_reason is not None for v in await store.messages_view(conv))

    assert await store.clear_blocked_reason(conv, "a7-client-side-id") is True
    assert all(v.blocked_reason is None for v in await store.messages_view(conv))
    # Nothing blocked left, so the fallback has nothing to find either.
    assert await store.clear_blocked_reason(conv, "a7-client-side-id") is False
    await store.stop()


async def test_a_second_stop_after_a_retired_one_stands_on_its_own(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # Retiring a marker must not be a one-shot: a thread the operator keeps resuming
    # stops, gets continued, and stops again — and the second stop has to raise its own
    # marker on its own turn, with the first one staying retired underneath it.
    toolset = _noop_toolset()
    _bounds_tool_calls(monkeypatch)
    store, db_engine = await _fresh_store(tmp_path)
    await store.start()
    conv = await store.create_conversation("operator", title="t")
    reg = RunRegistry()

    async def stopped_turn(prompt: str) -> str:
        """Run a turn that trips the tool-call bound; return its marked node id."""
        orch = build_chat_orchestrator(
            prompt,
            model=_calls_noop(),
            categories={"x": toolset},
            store=store,
            conversation_id=conv,
        )
        run = reg.submit(kind="chat", owner_id="operator", orchestrator=orch)
        await run.wait()
        assert run.status is RunStatus.blocked
        marked = [v for v in await store.messages_view(conv) if v.blocked_reason is not None]
        assert len(marked) == 1, "each stop raises exactly one marker"
        return marked[-1].id

    first = await stopped_turn("call the tool")
    assert await store.clear_blocked_reason(conv, first) is True

    # "Continue." — a plain turn on the same conversation, which stops again.
    second = await stopped_turn("Continue.")
    assert second != first, "the second stop marks its own turn, not the retired one"

    assert await store.clear_blocked_reason(conv, second) is True
    assert all(v.blocked_reason is None for v in await store.messages_view(conv))

    # Reload parity: neither retirement comes back, and the thread still has both turns.
    await store.stop()
    cold = ConversationStore(db_engine, await _unlocked_vault(tmp_path))
    await cold.start()
    cold_views = await cold.messages_view(conv)
    assert all(v.blocked_reason is None for v in cold_views)
    assert [v.content for v in cold_views if v.role == "user"] == ["call the tool", "Continue."]
    await cold.stop()


async def test_clearing_one_marker_leaves_an_earlier_one_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # Two stops can stand on one path at once (the operator continued the first with a
    # question of their own, and that stopped too). Clearing by id must retire exactly
    # the turn named — an earlier marker is still an unanswered prompt to resume.
    toolset = _noop_toolset()
    _bounds_tool_calls(monkeypatch)
    store, _ = await _fresh_store(tmp_path)
    await store.start()
    conv = await store.create_conversation("operator", title="t")
    reg = RunRegistry()

    async def stopped_turn(prompt: str) -> str:
        orch = build_chat_orchestrator(
            prompt,
            model=_calls_noop(),
            categories={"x": toolset},
            store=store,
            conversation_id=conv,
        )
        run = reg.submit(kind="chat", owner_id="operator", orchestrator=orch)
        await run.wait()
        return [v for v in await store.messages_view(conv) if v.blocked_reason is not None][-1].id

    first = await stopped_turn("call the tool")
    second = await stopped_turn("and again")
    assert {first, second} == {
        v.id for v in await store.messages_view(conv) if v.blocked_reason is not None
    }

    assert await store.clear_blocked_reason(conv, second) is True
    still_marked = {v.id for v in await store.messages_view(conv) if v.blocked_reason is not None}
    assert still_marked == {first}
    await store.stop()


async def test_the_fallback_retires_the_newest_stop_not_an_older_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # The fallback exists for a turn whose id the client made up locally, and a client
    # can only make one up for the turn it is streaming — the newest. With an older
    # marker also standing, the fallback must still take the newest, or continuing the
    # live stop would silently retire a stop further up the thread instead.
    toolset = _noop_toolset()
    _bounds_tool_calls(monkeypatch)
    store, _ = await _fresh_store(tmp_path)
    await store.start()
    conv = await store.create_conversation("operator", title="t")
    reg = RunRegistry()

    async def stopped_turn(prompt: str) -> str:
        orch = build_chat_orchestrator(
            prompt,
            model=_calls_noop(),
            categories={"x": toolset},
            store=store,
            conversation_id=conv,
        )
        run = reg.submit(kind="chat", owner_id="operator", orchestrator=orch)
        await run.wait()
        return [v for v in await store.messages_view(conv) if v.blocked_reason is not None][-1].id

    older = await stopped_turn("call the tool")
    newest = await stopped_turn("and again")

    assert await store.clear_blocked_reason(conv, "a9-client-side-id") is True
    marked = {v.id for v in await store.messages_view(conv) if v.blocked_reason is not None}
    assert marked == {older}, "the fallback took the newest stop, leaving the older one"
    assert newest not in marked
    await store.stop()
