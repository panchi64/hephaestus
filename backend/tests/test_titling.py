"""Auto-titling: the chassis names a fresh conversation from its first exchange."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from pydantic_ai import FunctionToolset, ToolApproved
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, DeltaToolCalls, FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.settings import ModelSettings

import agent.engine as engine
from agent import ParkedTurn, build_chat_orchestrator, build_resume_orchestrator
from agent.title import (
    MAX_TITLE_LEN,
    all_user_text,
    clean_title,
    first_user_text,
    generate_title,
    title_from_history,
)
from core.config import Settings
from core.db import init_db, make_engine
from core.vault import Vault
from runs import EventBody, Run, RunRegistry, RunStatus
from services.conversations import ConversationStore
from tools import RunDeps


async def _unlocked_vault(tmp_path: Path) -> Vault:
    vault = Vault(tmp_path / "keyfile.json")
    await vault.setup("pw")
    return vault


async def _fresh_store(tmp_path: Path) -> ConversationStore:
    engine_ = make_engine("sqlite:///:memory:")
    init_db(engine_)
    store = ConversationStore(engine_, await _unlocked_vault(tmp_path))
    await store.start()
    return store


def _bodies(run: Run) -> list[EventBody]:
    return [e.body for e in run.stream.replay()]


# --- sanitization -----------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ('"Trip Planning to Japan"', "Trip Planning to Japan"),
        ("Title: Debugging a Race Condition", "Debugging a Race Condition"),
        ("Resetting the Vault Password.", "Resetting the Vault Password"),
        ("  Multi\nLine\nReply  ", "Multi"),
        ("`Quoted Backticks`", "Quoted Backticks"),
        # The strips feed each other, so they run to a fixed point: here the quotes
        # are only reachable once the prefix is gone, and the trailing period only
        # once the closing quote is.
        ('Title: "Mock asyncio timers in pytest".', "Mock asyncio timers in pytest"),
        ("Thread: 'Cut the React bundle size'", "Cut the React bundle size"),
        ("   ", None),
        # A reasoning model the runtime didn't keep off inlines its thinking as a
        # <think> block; the title must come from the words after it, not the reasoning.
        ("<think>weigh the options</think>\nClean Title", "Clean Title"),
        (
            "<think>line one\nline two\nstill thinking</think>\nTitle After Reasoning",
            "Title After Reasoning",
        ),
        ("<think>only reasoning, no title</think>", None),
        # Casing is the model/template's choice, not ours — strip case-insensitively.
        ("<THINK>reasoning</THINK>\nCapitalized Tag Title", "Capitalized Tag Title"),
        # An unclosed block (model exhausted max_tokens mid-think; Pydantic AI still
        # returns the partial content) must strip to end-of-string, never leak a
        # half-thought as the title.
        ("<think>ran out of budget while still reasoning about the", None),
        ("<think>partial reasoning\nmore reasoning, no close tag", None),
    ],
)
def test_clean_sanitizes_model_replies(raw: str, expected: str | None):
    assert clean_title(raw) == expected


def test_clean_caps_length():
    title = clean_title("A " * 100)
    assert title is not None and len(title) <= MAX_TITLE_LEN


def test_clean_truncates_on_a_word_boundary():
    # A model that blew the word budget gets cut at the last whole word that fits —
    # a name is read at a glance, and a half word reads as a bug.
    long = "Investigating intermittent authentication failures across staging deployments"
    title = clean_title(long)
    assert title is not None and len(title) <= MAX_TITLE_LEN
    assert long.startswith(title)
    assert long[len(title)] == " "  # the cut landed between words, not inside one
    assert title.split() == long.split()[: len(title.split())]


def test_clean_hard_cuts_a_single_word_longer_than_the_cap():
    # No boundary to fall back to, so the cap still holds.
    title = clean_title("x" * 200)
    assert title == "x" * MAX_TITLE_LEN


# --- generation -------------------------------------------------------------


async def test_generate_title_returns_clean_title():
    model = TestModel(custom_output_text='"Configuring the Model Registry"')
    title = await generate_title(model, "how do I set up endpoints?")
    assert title == "Configuring the Model Registry"


async def test_generate_title_merges_reasoning_off_without_error():
    # The reasoning-off settings (from services.reasoning) are merged over the base
    # caps and passed through; a TestModel ignores them but the call still works.
    model = TestModel(custom_output_text="A Title")
    title = await generate_title(
        model,
        "q",
        reasoning_off={"extra_body": {"chat_template_kwargs": {"enable_thinking": False}}},
    )
    assert title == "A Title"


async def test_generate_title_strips_inlined_think_block():
    # A runtime that ignored the reasoning-off lever returns the think block inline in
    # the content; the title is read from after </think>, not from the reasoning.
    model = TestModel(custom_output_text="<think>let me think</think>\nGreat Title")
    title = await generate_title(model, "name this")
    assert title == "Great Title"


async def test_generate_title_applies_max_tokens_override():
    # The per-call max_tokens overrides the base cap so a think block has room to clear.
    captured: list[ModelSettings] = []

    async def capture(_messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        captured.append(info.model_settings or {})
        return ModelResponse(parts=[TextPart("A Title")])

    title = await generate_title(FunctionModel(capture), "q", max_tokens=4096)
    assert title == "A Title"
    assert captured[0].get("max_tokens") == 4096


# --- engine wiring ----------------------------------------------------------


async def test_first_turn_emits_and_persists_title(tmp_path: Path):
    store = await _fresh_store(tmp_path)
    conv = await store.create_conversation("operator")  # untitled
    reg = RunRegistry()

    orch = build_chat_orchestrator(
        "plan a trip to Japan",
        model=TestModel(custom_output_text="Sure, here is a plan."),
        title_model=TestModel(custom_output_text="Japan Trip Plan"),
        categories={},
        store=store,
        conversation_id=conv,
    )
    run = reg.submit(kind="chat", owner_id="operator", orchestrator=orch)
    _ = await run.wait()

    titled = [b for b in _bodies(run) if b.type == "conversation.titled"]
    assert len(titled) == 1
    assert titled[0].conversation_id == conv
    assert titled[0].title == "Japan Trip Plan"

    # Emitted before the run ends, so a still-open stream carries it.
    types = [b.type for b in _bodies(run)]
    assert types.index("conversation.titled") < types.index("run.ended")

    # And it was persisted, not just announced.
    summary = await store.get_summary(conv, "operator")
    assert summary is not None and summary.title == "Japan Trip Plan"
    await store.stop()


async def test_continued_turn_is_not_retitled(tmp_path: Path):
    store = await _fresh_store(tmp_path)
    conv = await store.create_conversation("operator")
    reg = RunRegistry()

    def run_turn(prompt: str) -> Run:
        orch = build_chat_orchestrator(
            prompt,
            model=TestModel(custom_output_text="ok"),
            title_model=TestModel(custom_output_text="Some Title"),
            categories={},
            store=store,
            conversation_id=conv,
        )
        return reg.submit(kind="chat", owner_id="operator", orchestrator=orch)

    first = run_turn("first message")
    _ = await first.wait()
    second = run_turn("second message")
    _ = await second.wait()

    # Only the opening turn names the thread; the continuation does not re-title.
    assert any(b.type == "conversation.titled" for b in _bodies(first))
    assert not any(b.type == "conversation.titled" for b in _bodies(second))
    await store.stop()


async def test_existing_operator_title_is_never_clobbered(tmp_path: Path):
    # A thread the operator already named must not be auto-renamed even on its
    # first turn — set_title_if_absent is the authoritative guard.
    store = await _fresh_store(tmp_path)
    conv = await store.create_conversation("operator", title="My Own Name")
    reg = RunRegistry()

    orch = build_chat_orchestrator(
        "hello",
        model=TestModel(custom_output_text="hi"),
        title_model=TestModel(custom_output_text="Auto Generated"),
        categories={},
        store=store,
        conversation_id=conv,
    )
    run = reg.submit(kind="chat", owner_id="operator", orchestrator=orch)
    _ = await run.wait()

    assert not any(b.type == "conversation.titled" for b in _bodies(run))
    summary = await store.get_summary(conv, "operator")
    assert summary is not None and summary.title == "My Own Name"
    await store.stop()


async def test_titling_skipped_without_title_model(tmp_path: Path):
    store = await _fresh_store(tmp_path)
    conv = await store.create_conversation("operator")
    reg = RunRegistry()

    orch = build_chat_orchestrator(
        "hello",
        model=TestModel(custom_output_text="hi"),
        categories={},
        store=store,
        conversation_id=conv,
    )
    run = reg.submit(kind="chat", owner_id="operator", orchestrator=orch)
    _ = await run.wait()

    assert not any(b.type == "conversation.titled" for b in _bodies(run))
    summary = await store.get_summary(conv, "operator")
    assert summary is not None and summary.title is None
    await store.stop()


async def test_titling_skipped_when_disabled_in_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(engine, "get_settings", lambda: Settings(title_enabled=False))
    store = await _fresh_store(tmp_path)
    conv = await store.create_conversation("operator")
    reg = RunRegistry()

    orch = build_chat_orchestrator(
        "hello",
        model=TestModel(custom_output_text="hi"),
        title_model=TestModel(custom_output_text="Should Not Appear"),
        categories={},
        store=store,
        conversation_id=conv,
    )
    run = reg.submit(kind="chat", owner_id="operator", orchestrator=orch)
    _ = await run.wait()

    assert not any(b.type == "conversation.titled" for b in _bodies(run))
    await store.stop()


async def test_parked_first_turn_is_titled_exactly_once(tmp_path: Path):
    # A first turn whose opening message triggers an approval-gated tool parks, then
    # resumes to completion — it must be named exactly once across the two halves. The
    # concurrent namer usually gets there first (a thread waiting on an approval shows
    # its name rather than sitting "Untitled" while the operator decides); when the park
    # beats it, the resume names it from history instead. Either way: one announcement,
    # never two, and never a name persisted without one.
    store = await _fresh_store(tmp_path)
    conv = await store.create_conversation("operator")

    toolset: FunctionToolset[RunDeps] = FunctionToolset()

    @toolset.tool_plain(requires_approval=True)
    def delete_thing(name: str) -> str:
        return f"deleted {name}"

    def _tool_ran(messages: list[ModelMessage]) -> bool:
        return any(
            type(part).__name__ == "ToolReturnPart"
            for message in messages
            for part in message.parts
        )

    async def stream_fn(
        messages: list[ModelMessage], info: AgentInfo
    ) -> AsyncIterator[str | DeltaToolCalls]:
        if _tool_ran(messages):
            yield "Done deleting the thing."
        else:
            yield {0: DeltaToolCall(name=info.function_tools[0].name, json_args='{"name": "x"}')}

    reg = RunRegistry()
    orch = build_chat_orchestrator(
        "please delete the thing",
        model=FunctionModel(stream_function=stream_fn),
        categories={"danger": toolset},
        title_model=TestModel(custom_output_text="Deleting The Thing"),
        store=store,
        conversation_id=conv,
    )
    run = reg.submit(kind="chat", owner_id="operator", orchestrator=orch)
    _ = await run.wait()

    assert run.status is RunStatus.awaiting_input
    # Whichever half named it, the announcement and the stored name agree — a name in
    # the database with no event on the stream is the failure this guards.
    parked_titles = [b for b in _bodies(run) if b.type == "conversation.titled"]
    summary = await store.get_summary(conv, "operator")
    assert summary is not None
    assert (summary.title == "Deleting The Thing") == bool(parked_titles)

    parked = run.parked_payload
    assert isinstance(parked, ParkedTurn)
    call_id = parked.requests.approvals[0].tool_call_id
    _ = await reg.resume(
        run.id, build_resume_orchestrator(parked, {call_id: ToolApproved()}, store=store)
    )
    _ = await run.wait()
    assert run.status is RunStatus.done

    # Named on the same stream, exactly once across both halves — `set_title_if_absent`
    # is the guard that stops the resume re-announcing what the concurrent namer already did.
    titled = [b for b in _bodies(run) if b.type == "conversation.titled"]
    assert len(titled) == 1 and titled[0].title == "Deleting The Thing"
    summary = await store.get_summary(conv, "operator")
    assert summary is not None and summary.title == "Deleting The Thing"
    await store.stop()


async def test_titling_runs_concurrently_with_the_answer(tmp_path: Path):
    # Titling is kicked off up-front and overlaps the answer rather than following
    # it, so it adds no post-answer "writing" tail. Proven structurally: the answer
    # only completes once titling has begun — a deadlock unless the two run
    # concurrently — yet the title is still announced before the run ends.
    store = await _fresh_store(tmp_path)
    conv = await store.create_conversation("operator")
    reg = RunRegistry()

    title_started = asyncio.Event()

    def title_fn(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        title_started.set()
        return ModelResponse(parts=[TextPart("Concurrent Title")])

    async def answer_stream(_messages: list[ModelMessage], _info: AgentInfo) -> AsyncIterator[str]:
        _ = await asyncio.wait_for(title_started.wait(), timeout=2)
        yield "answer"

    orch = build_chat_orchestrator(
        "name me",
        model=FunctionModel(stream_function=answer_stream),
        title_model=FunctionModel(function=title_fn),
        categories={},
        store=store,
        conversation_id=conv,
    )
    run = reg.submit(kind="chat", owner_id="operator", orchestrator=orch)
    _ = await run.wait()

    assert run.status is RunStatus.done
    titled = [b for b in _bodies(run) if b.type == "conversation.titled"]
    assert len(titled) == 1 and titled[0].title == "Concurrent Title"
    types = [b.type for b in _bodies(run)]
    assert types.index("conversation.titled") < types.index("run.ended")
    await store.stop()


async def test_title_is_announced_while_the_answer_is_still_streaming(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # Generating concurrently isn't enough: the name must also be *announced* the moment
    # it lands, not held until the turn finishes. Otherwise a long tool-using turn leaves
    # the thread visibly unnamed for its whole duration even though the name was ready in
    # the first second. Proven structurally — the answer can't finish until the title has
    # been persisted+emitted, which deadlocks if the announcement waits on the answer.
    store = await _fresh_store(tmp_path)
    conv = await store.create_conversation("operator")
    reg = RunRegistry()

    announced = asyncio.Event()
    set_title = store.set_title_if_absent

    async def hooked(conversation_id: str, name: str) -> bool:
        stored = await set_title(conversation_id, name)
        announced.set()
        return stored

    monkeypatch.setattr(store, "set_title_if_absent", hooked)

    def title_fn(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart("Named Mid Answer")])

    async def answer_stream(_messages: list[ModelMessage], _info: AgentInfo) -> AsyncIterator[str]:
        yield "still "
        _ = await asyncio.wait_for(announced.wait(), timeout=2)
        yield "writing"

    orch = build_chat_orchestrator(
        "name me",
        model=FunctionModel(stream_function=answer_stream),
        title_model=FunctionModel(function=title_fn),
        categories={},
        store=store,
        conversation_id=conv,
    )
    run = reg.submit(kind="chat", owner_id="operator", orchestrator=orch)
    _ = await run.wait()

    assert run.status is RunStatus.done
    types = [b.type for b in _bodies(run)]
    # The name landed before the answer's last token, not after the whole turn.
    assert types.index("conversation.titled") < len(types) - 1 - types[::-1].index("answer.delta")
    await store.stop()


async def test_concurrent_title_is_cancelled_when_the_turn_fails(tmp_path: Path):
    # The title is kicked off concurrently *before* the answer; if the turn then
    # raises, the orchestrator must cancel it so the title-model call doesn't run on
    # detached past the failed run (and no stale title is announced).
    store = await _fresh_store(tmp_path)
    conv = await store.create_conversation("operator")
    reg = RunRegistry()

    title_running = asyncio.Event()
    cancelled = asyncio.Event()
    release = asyncio.Event()  # never set — the title stays in-flight until cancelled

    async def title_fn(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        title_running.set()
        try:
            _ = await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return ModelResponse(parts=[TextPart("Should Not Appear")])

    async def fail_once_title_runs() -> str:
        _ = await title_running.wait()  # ensure the title is in-flight first
        raise RuntimeError("main model failed")

    async def boom_stream(_messages: list[ModelMessage], _info: AgentInfo) -> AsyncIterator[str]:
        yield await fail_once_title_runs()

    orch = build_chat_orchestrator(
        "name me",
        model=FunctionModel(stream_function=boom_stream),
        title_model=FunctionModel(function=title_fn),
        categories={},
        store=store,
        conversation_id=conv,
    )
    run = reg.submit(kind="chat", owner_id="operator", orchestrator=orch)
    _ = await run.wait()

    assert run.status is RunStatus.error
    _ = await asyncio.wait_for(cancelled.wait(), timeout=2)  # the title task was cancelled
    assert not any(b.type == "conversation.titled" for b in _bodies(run))
    await store.stop()


# --- history extraction -----------------------------------------------------


async def test_title_text_extraction_from_history(tmp_path: Path):
    # The message the namer sees is read back from persisted history, so the
    # extraction helper must pull the first user prompt — the title is named for
    # what the operator asked, not the assistant's reply.
    store = await _fresh_store(tmp_path)
    conv = await store.create_conversation("operator")
    reg = RunRegistry()
    orch = build_chat_orchestrator(
        "what is the capital of France?",
        model=TestModel(custom_output_text="Paris is the capital."),
        categories={},
        store=store,
        conversation_id=conv,
    )
    run = reg.submit(kind="chat", owner_id="operator", orchestrator=orch)
    _ = await run.wait()

    history = await store.history(conv)
    assert "capital of France" in first_user_text(history)
    await store.stop()


def test_all_user_text_joins_every_operator_turn_only():
    # A manual re-title is named for the whole arc the operator asked across, so the
    # extraction joins *every* user prompt in order — but still never the assistant's
    # replies, keeping the small title model off non-operator content.
    history = [
        ModelRequest(parts=[UserPromptPart(content="first question about cats")]),
        ModelResponse(parts=[TextPart(content="an answer about cats")]),
        ModelRequest(parts=[UserPromptPart(content="follow-up about dogs")]),
    ]
    joined = all_user_text(history)
    assert "first question about cats" in joined
    assert "follow-up about dogs" in joined
    assert "an answer about cats" not in joined


async def test_title_from_history_scope_picks_opening_vs_full():
    # The auto-titler (default) names from the opening turn only; the manual re-title
    # (full=True) spans every operator turn. One shared extraction→generate step, the
    # only difference being which turns feed it.
    history = [
        ModelRequest(parts=[UserPromptPart(content="set up a vite project")]),
        ModelResponse(parts=[TextPart(content="ok")]),
        ModelRequest(parts=[UserPromptPart(content="now add tailwind and a router")]),
    ]

    seen: list[str] = []

    def record(messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        last = messages[-1]
        assert isinstance(last, ModelRequest)
        part = last.parts[-1]
        assert isinstance(part, UserPromptPart)
        assert isinstance(part.content, str)
        seen.append(part.content)
        return ModelResponse(parts=[TextPart(content="A Title")])

    model = FunctionModel(record)
    assert await title_from_history(model, history) == "A Title"
    assert "set up a vite project" in seen[-1]
    assert "now add tailwind" not in seen[-1]

    assert await title_from_history(model, history, full=True) == "A Title"
    assert "now add tailwind" in seen[-1]


async def test_title_from_history_empty_history_is_none():
    assert await title_from_history(TestModel(custom_output_text="x"), []) is None
