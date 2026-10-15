"""A turn's prompt is written down when the turn begins, not when it ends.

The whole turn used to reach the store in one write at the finish, so a thread opened while
a run was in flight had no prompt for it, a cancel before the first token took the
operator's own words with it, and the client had to invent a bubble for the question. Now
the prompt is recorded first, the run names it (`Run.prompt_message_id` →
`ActiveRun.prompt_message_id`), and everything afterwards — the finish, a park's resume,
the stop-flush hooks — records only what followed it.

These tests run the real orchestrator against a real store, because the failures that
matter are the seams: a prompt recorded twice, a stamp landing on a steering message, a
regenerate that writes the question again, a verifier correction dropping the wrong range.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping, Sequence
from pathlib import Path

from pydantic_ai import FunctionToolset, ModelRequest, ModelResponse, TextPart, ToolApproved
from pydantic_ai.messages import (
    ModelMessage,
    RetryPromptPart,
    SystemPromptPart,
    ThinkingPart,
    ToolReturnPart,
    UserContent,
    UserPromptPart,
)
from pydantic_ai.models import Model
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.toolsets import AbstractToolset
from sqlalchemy import Engine

from agent import ParkedTurn, build_chat_orchestrator, build_resume_orchestrator
from agent.finalize import finalize
from agent.flush import PersistContext
from agent.history import TurnStart, after_recorded_prompt
from agent.turn import TurnResult
from core.db import init_db, make_engine
from core.vault import Vault
from routes.deps import OPERATOR_ID, app_state
from runs import Run, RunRegistry, RunStatus, RunStream
from services.conversations import ConversationStore
from tools import RunDeps

from ._helpers import client_app
from ._json import arr, json_body, obj


async def _fresh_store(tmp_path: Path) -> tuple[ConversationStore, Engine]:
    vault = Vault(tmp_path / "keyfile.json")
    await vault.setup("pw")
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    return ConversationStore(engine, vault), engine


def _requests(store: ConversationStore, conversation_id: str) -> list[str]:
    """The text of every operator-prompt node in the tree, on any branch."""
    tree = store.cache_get(conversation_id)
    assert tree is not None
    return [
        _text(part.content)
        for node in sorted(tree.nodes.values(), key=lambda n: n.seq)
        if isinstance(node.message, ModelRequest) and not node.compacted
        for part in node.message.parts
        if isinstance(part, UserPromptPart)
    ]


def _text(content: str | Sequence[UserContent]) -> str:
    assert isinstance(content, str)
    return content


def _answering(answer: str) -> TestModel:
    return TestModel(custom_output_text=answer, call_tools=[])


def _hanging_model(reached: asyncio.Event) -> FunctionModel:
    """A model that is asked, signals it, and never answers."""

    async def stream(_messages: list[ModelMessage], _info: AgentInfo) -> AsyncIterator[str]:
        reached.set()
        _ = await asyncio.Event().wait()
        yield "unreachable"

    return FunctionModel(stream_function=stream)


def _approval_toolset() -> FunctionToolset[RunDeps]:
    toolset: FunctionToolset[RunDeps] = FunctionToolset()

    @toolset.tool_plain(requires_approval=True)
    def delete_thing(name: str) -> str:
        return f"deleted {name}"

    return toolset


def _submit(
    reg: RunRegistry,
    store: ConversationStore,
    conv: str,
    prompt: str | None,
    model: Model,
    categories: Mapping[str, AbstractToolset[RunDeps]] | None = None,
) -> Run:
    orch = build_chat_orchestrator(
        prompt, model=model, categories={} if categories is None else categories, store=store,
        conversation_id=conv,
    )  # fmt: skip
    return reg.submit(kind="chat", owner_id="operator", orchestrator=orch, conversation_id=conv)


# --- the detail, mid-run -------------------------------------------------------


async def test_the_prompt_is_on_record_and_named_while_the_run_is_in_flight():
    async with client_app() as (client, app):
        store = app_state(app).conversations
        cid = await store.create_conversation(OPERATOR_ID)
        reached, release = asyncio.Event(), asyncio.Event()

        async def stream(_messages: list[ModelMessage], _info: AgentInfo) -> AsyncIterator[str]:
            reached.set()
            _ = await release.wait()
            yield "the answer"

        orch = build_chat_orchestrator(
            "hello", model=FunctionModel(stream_function=stream), categories={},
            store=store, conversation_id=cid,
        )  # fmt: skip
        run = app_state(app).runs.submit(
            kind="chat", owner_id=OPERATOR_ID, orchestrator=orch, conversation_id=cid
        )
        _ = await asyncio.wait_for(reached.wait(), 5)

        detail = json_body(await client.get(f"/conversations/{cid}"))
        assert [(obj(m)["role"], obj(m)["content"]) for m in arr(detail["messages"])] == [
            ("user", "hello")
        ]
        active_run = obj(detail["active_run"])
        assert active_run["id"] == run.id
        prompt_id = obj(arr(detail["messages"])[0])["id"]
        assert active_run["prompt_message_id"] == prompt_id

        release.set()
        await run.wait()
        done = json_body(await client.get(f"/conversations/{cid}"))
        assert [obj(m)["role"] for m in arr(done["messages"])] == ["user", "assistant"]
        # The same row before and after: the id the client held mid-run is still valid.
        assert obj(arr(done["messages"])[0])["id"] == prompt_id
        assert done["active_run"] is None


# --- finalize records only the remainder -----------------------------------------


async def test_finalize_appends_the_answer_without_a_second_copy_of_the_prompt(tmp_path: Path):
    store, _ = await _fresh_store(tmp_path)
    await store.start()
    conv = await store.create_conversation("operator")
    run = _submit(RunRegistry(), store, conv, "hello", _answering("hi there"))
    await run.wait()

    assert _requests(store, conv) == ["hello"]
    views = await store.messages_view(conv)
    assert [(v.role, v.content) for v in views] == [("user", "hello"), ("assistant", "hi there")]
    assert views[0].id == run.prompt_message_id
    await store.stop()


async def test_the_prompts_stamps_land_on_it_and_never_on_a_steering_message(tmp_path: Path):
    store, _ = await _fresh_store(tmp_path)
    await store.start()
    conv = await store.create_conversation("operator")

    prompt_id = store.record_prompt(
        conv, ModelRequest(parts=[UserPromptPart("look at @a.py")]), file_refs=["a.py"]
    )
    _ = store.record(
        conv,
        [
            ModelRequest(parts=[UserPromptPart("and one more thing")]),
            ModelResponse(parts=[TextPart("ok")]),
        ],
        file_refs=["not-for-this-message"],
        prompt_id=prompt_id,
    )

    views = await store.messages_view(conv)
    assert [v.role for v in views] == ["user", "user", "assistant"]
    assert views[0].file_refs == ["a.py"]
    assert views[1].file_refs == []
    await store.stop()


async def test_an_opening_request_carrying_steering_yields_it_as_its_own_request():
    opening = ModelRequest(parts=[UserPromptPart("the prompt"), UserPromptPart("steered in")])
    response = ModelResponse(parts=[TextPart("answer")])

    rest = after_recorded_prompt([opening, response])

    assert [type(m).__name__ for m in rest] == ["ModelRequest", "ModelResponse"]
    assert isinstance(rest[0], ModelRequest)
    steered = rest[0].parts[0]
    assert isinstance(steered, UserPromptPart) and steered.content == "steered in"
    assert len(after_recorded_prompt([opening])) == 1  # the steering, with no response yet
    assert after_recorded_prompt([ModelRequest(parts=[UserPromptPart("only")])]) == []
    assert after_recorded_prompt([]) == []


# --- the verifier's drop range -------------------------------------------------


async def test_a_verifier_drop_range_still_drops_the_rejected_answer_and_the_nudge(tmp_path: Path):
    """`clean_drop` is a pair of absolute indices into the turn's whole message list. The
    prompt being set aside happens after the range is applied, so the indices keep meaning
    what the verifier took them to mean — pinned here because shifting the start past the
    prompt is the obvious way to get it wrong."""
    store, _ = await _fresh_store(tmp_path)
    await store.start()
    conv = await store.create_conversation("operator")
    _ = store.record(
        conv,
        [ModelRequest(parts=[UserPromptPart("earlier")]), ModelResponse(parts=[TextPart("old")])],
    )
    prompt_id = store.record_prompt(conv, ModelRequest(parts=[UserPromptPart("do it")]))

    messages = [
        ModelRequest(parts=[UserPromptPart("earlier")]),
        ModelResponse(parts=[TextPart("old")]),
        ModelRequest(parts=[UserPromptPart("do it")]),  # 2 — the recorded prompt
        ModelResponse(parts=[TextPart("rejected answer")]),  # 3
        ModelRequest(parts=[UserPromptPart("please redo")]),  # 4 — the synthetic nudge
        ModelResponse(parts=[TextPart("corrected answer")]),  # 5
    ]
    run = Run(id="r", kind="chat", owner_id="operator", stream=RunStream())
    finalize(
        run,
        TurnResult(answer="corrected answer", messages=messages, clean_drop=(3, 4)),
        store=store,
        context=PersistContext(
            conversation_id=conv,
            start=TurnStart(2, 0, prompt_id),
            clean_drop=(3, 4),
        ),
    )

    views = await store.messages_view(conv)
    assert [(v.role, v.content) for v in views] == [
        ("user", "earlier"),
        ("assistant", "old"),
        ("user", "do it"),
        ("assistant", "corrected answer"),
    ]
    assert _requests(store, conv) == ["earlier", "do it"]
    await store.stop()


# --- stops ---------------------------------------------------------------------


async def test_a_cancel_before_the_first_token_keeps_the_prompt_and_its_marker(tmp_path: Path):
    store, db_engine = await _fresh_store(tmp_path)
    await store.start()
    conv = await store.create_conversation("operator")
    reached = asyncio.Event()
    reg = RunRegistry()
    run = _submit(reg, store, conv, "never answered", _hanging_model(reached))
    _ = await asyncio.wait_for(reached.wait(), 5)

    assert await reg.cancel(run.id) is True
    await run.wait()

    assert run.status is RunStatus.cancelled
    views = await store.messages_view(conv)
    assert [(v.role, v.content) for v in views] == [("user", "never answered")]
    assert views[0].id == run.prompt_message_id
    assert views[0].blocked_reason == "cancelled by the operator"

    # And the marker is durable, not just in the warm tree.
    await store.stop()
    cold = ConversationStore(db_engine, store.vault)
    await cold.start()
    cold_views = await cold.messages_view(conv)
    assert [(v.role, v.content) for v in cold_views] == [("user", "never answered")]
    assert cold_views[0].blocked_reason == "cancelled by the operator"
    await cold.stop()


async def test_a_park_holds_the_prompt_and_the_resume_adds_only_the_rest(tmp_path: Path):
    store, _ = await _fresh_store(tmp_path)
    await store.start()
    conv = await store.create_conversation("operator")
    reg = RunRegistry()
    run = _submit(
        reg, store, conv, "delete the thing", TestModel(custom_output_text="done"),
        categories={"danger": _approval_toolset()},
    )  # fmt: skip
    await run.wait()
    assert run.status is RunStatus.awaiting_input

    # Parked: the prompt is on record and named; nothing else is.
    assert [v.role for v in await store.messages_view(conv)] == ["user"]
    parked = run.parked_payload
    assert isinstance(parked, ParkedTurn)
    assert parked.prompt_message_id == run.prompt_message_id is not None

    call_id = parked.requests.approvals[0].tool_call_id
    _ = await reg.resume(
        run.id, build_resume_orchestrator(parked, {call_id: ToolApproved()}, store=store)
    )
    await run.wait()

    assert run.status is RunStatus.done
    assert _requests(store, conv) == ["delete the thing"]
    views = await store.messages_view(conv)
    assert [v.role for v in views] == ["user", "assistant"]
    assert views[0].id == run.prompt_message_id
    await store.stop()


async def test_cancelling_a_parked_run_keeps_the_prompt_and_the_partial_turn(tmp_path: Path):
    store, _ = await _fresh_store(tmp_path)
    await store.start()
    conv = await store.create_conversation("operator")
    reg = RunRegistry()
    run = _submit(
        reg, store, conv, "delete the thing", TestModel(custom_output_text="done"),
        categories={"danger": _approval_toolset()},
    )  # fmt: skip
    await run.wait()
    assert run.status is RunStatus.awaiting_input

    assert await reg.cancel(run.id) is True

    assert _requests(store, conv) == ["delete the thing"]
    views = await store.messages_view(conv)
    assert [v.role for v in views] == ["user", "assistant"]
    assert views[0].id == run.prompt_message_id
    assert views[-1].blocked_reason == "cancelled by the operator"
    await store.stop()


# --- steering ---------------------------------------------------------------------


async def test_a_message_sent_mid_run_persists_behind_the_prompt_and_before_the_rest(
    tmp_path: Path,
):
    store, _ = await _fresh_store(tmp_path)
    await store.start()
    conv = await store.create_conversation("operator")
    toolset: FunctionToolset[RunDeps] = FunctionToolset()
    started, proceed = asyncio.Event(), asyncio.Event()

    @toolset.tool_plain(metadata={"sensitivity": "read"})
    async def slow(x: int) -> int:
        started.set()
        _ = await proceed.wait()
        return x

    run = _submit(
        RunRegistry(), store, conv, "call the tool", TestModel(call_tools=["x_slow"]),
        categories={"x": toolset},
    )  # fmt: skip
    _ = await started.wait()
    _ = run.enqueue_message("also check the other one")
    proceed.set()
    await run.wait()

    assert run.status is RunStatus.done
    assert _requests(store, conv) == ["call the tool", "also check the other one"]
    views = await store.messages_view(conv)
    users = [v for v in views if v.role == "user"]
    assert [u.content for u in users] == ["call the tool", "also check the other one"]
    assert views[0].id == run.prompt_message_id
    assert views[-1].role == "assistant"
    await store.stop()


# --- branching: exactly one prompt write per path ------------------------------------


async def test_a_regenerate_reuses_the_prompt_and_names_it(tmp_path: Path):
    store, _ = await _fresh_store(tmp_path)
    await store.start()
    reg = RunRegistry()
    conv = await store.create_conversation("operator")
    first = _submit(reg, store, conv, "hello", _answering("first"))
    await first.wait()
    views = await store.messages_view(conv)
    prompt_id, answer_id = views[0].id, views[1].id

    assert await store.regenerate_point(conv, answer_id)
    second = _submit(reg, store, conv, None, _answering("second"))
    await second.wait()

    assert _requests(store, conv) == ["hello"]  # still one copy in the whole tree
    assert second.prompt_message_id == prompt_id  # it names the request it re-answered
    views = await store.messages_view(conv)
    assert [v.role for v in views] == ["user", "assistant"]
    assert views[0].id == prompt_id
    assert views[1].version_count == 2
    await store.stop()


async def test_an_edit_records_one_new_version_of_the_prompt(tmp_path: Path):
    store, _ = await _fresh_store(tmp_path)
    await store.start()
    reg = RunRegistry()
    conv = await store.create_conversation("operator")
    await _submit(reg, store, conv, "hello", _answering("first")).wait()
    original = (await store.messages_view(conv))[0].id

    assert await store.edit_point(conv, original)
    edited = _submit(reg, store, conv, "hello, edited", _answering("second"))
    await edited.wait()

    assert _requests(store, conv) == ["hello", "hello, edited"]  # one new write
    views = await store.messages_view(conv)
    assert [(v.role, v.content) for v in views] == [
        ("user", "hello, edited"),
        ("assistant", "second"),
    ]
    assert views[0].version_count == 2
    assert views[0].id == edited.prompt_message_id != original
    await store.stop()


# --- the compaction plan ---------------------------------------------------------------


async def test_a_fold_never_swallows_the_prompt_being_answered(tmp_path: Path):
    store, _ = await _fresh_store(tmp_path)
    await store.start()
    conv = await store.create_conversation("operator")
    _ = store.record(
        conv,
        [ModelRequest(parts=[UserPromptPart("earlier")]), ModelResponse(parts=[TextPart("old")])],
    )
    _ = store.record_prompt(conv, ModelRequest(parts=[UserPromptPart("in flight")]))

    plan = await store.compaction_plan(conv)

    assert plan is not None
    texts = [
        part.content
        for message in plan.messages
        for part in message.parts
        if isinstance(
            part,
            UserPromptPart
            | TextPart
            | SystemPromptPart
            | ToolReturnPart
            | RetryPromptPart
            | ThinkingPart,
        )
    ]
    assert texts == ["earlier", "old"]  # the prompt stays out of the summary
    await store.stop()
