"""Mid-run steering: messages sent while a run executes queue on the Run and are
injected at the next model-request boundary (or auto-continue a finished turn),
then persist as ordinary user messages in the conversation tree."""

from __future__ import annotations

import asyncio
import json

from pydantic_ai import FunctionToolset, RunContext, ToolApproved
from pydantic_ai.messages import ToolReturnPart, UserPromptPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from pydantic_ai.models.test import TestModel

from agent import ParkedTurn, build_chat_orchestrator, build_resume_orchestrator
from runs import Run, RunRegistry, RunStatus, RunStream
from tools import RunDeps

from .test_branching import _fresh_store

OWNER = "operator"


def _bodies(run):
    return [e.body for e in run.stream.replay()]


def _types(run):
    return [b.type for b in _bodies(run)]


def _injected_user_texts(messages) -> list[str]:
    """The user-prompt texts riding the latest (tool-return) request."""
    return [
        p.content
        for p in messages[-1].parts
        if isinstance(p, UserPromptPart) and isinstance(p.content, str)
    ]


def _steering_toolset(queue_text: str) -> FunctionToolset[RunDeps]:
    """One tool that queues a steering message onto its own run mid-turn — the
    enqueue lands while the model's tool batch executes, exactly the window the
    live product sees between two model requests."""
    toolset: FunctionToolset[RunDeps] = FunctionToolset()

    # Declares its class: this toolset is composed here rather than shipped in the
    # catalog, so the name registry has never heard of `t_poke` and would otherwise gate
    # it as the furthest-reaching thing it could be. The steering under test happens at
    # the ordinary level, so the tool has to say it only reads.
    @toolset.tool(metadata={"sensitivity": "read"})
    async def poke(ctx: RunContext[RunDeps]) -> str:
        # Async so it runs on the loop (like the real tools): enqueue emits on the
        # run's stream, which needs the running loop for its activity stamp.
        ctx.deps.run.enqueue_message(queue_text)
        return "poked"

    return toolset


def _call_then_report(tool_name: str = "t_poke"):
    """First request: call the tool. Second request: answer with the user texts
    found on the incoming (tool-return) request, so the assertion reads straight
    off the answer what the model was actually shown."""

    async def stream_fn(messages, info):
        settled = any(isinstance(p, ToolReturnPart) for m in messages for p in m.parts)
        if not settled:
            yield {0: DeltaToolCall(name=tool_name, json_args=json.dumps({}))}
        else:
            yield "saw: " + "|".join(_injected_user_texts(messages))

    return stream_fn


async def test_midloop_injection_rides_next_model_request():
    reg = RunRegistry()
    orch = build_chat_orchestrator(
        "hello",
        model=FunctionModel(stream_function=_call_then_report()),
        categories={"t": _steering_toolset("and also this")},
    )
    run = reg.submit(kind="chat", owner_id=OWNER, orchestrator=orch)
    await run.wait()

    assert run.status is RunStatus.done
    # The queued message rode the second model request as a user part.
    answer = "".join(b.text for b in _bodies(run) if b.type == "answer.delta")
    assert answer == "saw: and also this"
    # Queue → inject, in order, and the inject lands before the second step opens.
    types = _types(run)
    assert types.index("message.queued") < types.index("message.injected")
    second_step = types.index("step.started", types.index("step.completed"))
    assert types.index("message.injected") < second_step
    assert run.pending_messages == []


async def test_end_of_run_queued_message_autocontinues_same_run():
    # The message lands while the model is producing its final answer (no tool
    # loop left to inject into) — the turn must continue as a new segment on the
    # same run instead of dropping it.
    async def stream_fn(messages, info):
        user_texts = [
            p.content
            for m in messages
            for p in m.parts
            if isinstance(p, UserPromptPart) and isinstance(p.content, str)
        ]
        if "more please" not in user_texts:
            run_box["run"].enqueue_message("more please")
            yield "first answer"
        else:
            yield "second answer"

    run_box: dict[str, Run] = {}
    reg = RunRegistry()
    orch = build_chat_orchestrator(
        "hello", model=FunctionModel(stream_function=stream_fn), categories={}
    )
    run_box["run"] = reg.submit(kind="chat", owner_id=OWNER, orchestrator=orch)
    run = run_box["run"]
    await run.wait()

    assert run.status is RunStatus.done
    answer = "".join(b.text for b in _bodies(run) if b.type == "answer.delta")
    assert answer == "first answersecond answer"
    types = _types(run)
    assert types.count("step.started") == 2  # one segment per answer
    assert "message.queued" in types and "message.injected" in types
    assert types.count("run.ended") == 1  # one run end to end


async def test_injected_message_persists_as_its_own_user_turn(tmp_path):
    store, _ = await _fresh_store(tmp_path)
    await store.start()
    reg = RunRegistry()
    conv = await store.create_conversation(OWNER)

    orch = build_chat_orchestrator(
        "hello",
        model=FunctionModel(stream_function=_call_then_report()),
        categories={"t": _steering_toolset("and also this")},
        store=store,
        conversation_id=conv,
    )
    run = reg.submit(kind="chat", owner_id=OWNER, orchestrator=orch)
    await run.wait()
    assert run.status is RunStatus.done

    view = await store.messages_view(conv)
    # The injected message is split out of the tool-return request into its own
    # tree node: a normal user bubble between the two assistant segments.
    assert [m.role for m in view] == ["user", "assistant", "user", "assistant"]
    assert view[0].content == "hello"
    assert view[2].content == "and also this"
    assert view[3].content == "saw: and also this"
    await store.stop()


async def test_queue_while_parked_is_injected_on_resume():
    toolset: FunctionToolset[RunDeps] = FunctionToolset()

    @toolset.tool_plain(requires_approval=True)
    def delete_thing() -> str:
        return "deleted"

    async def stream_fn(messages, info):
        settled = any(isinstance(p, ToolReturnPart) for m in messages for p in m.parts)
        if not settled:
            yield {0: DeltaToolCall(name="danger_delete_thing", json_args=json.dumps({}))}
        else:
            yield "saw: " + "|".join(_injected_user_texts(messages))

    reg = RunRegistry()
    orch = build_chat_orchestrator(
        "delete it",
        model=FunctionModel(stream_function=stream_fn),
        categories={"danger": toolset},
    )
    run = reg.submit(kind="chat", owner_id=OWNER, orchestrator=orch)
    for _ in range(200):
        if run.status is RunStatus.awaiting_input:
            break
        await asyncio.sleep(0.01)
    assert run.status is RunStatus.awaiting_input

    # Queued while parked: sits in the inbox (the parked task has fully exited)…
    run.enqueue_message("while you were parked")
    assert [m.text for m in run.pending_messages] == ["while you were parked"]

    parked = run.parked_payload
    assert isinstance(parked, ParkedTurn)
    call_id = parked.requests.approvals[0].tool_call_id
    resumed = await reg.resume(run.id, build_resume_orchestrator(parked, {call_id: ToolApproved()}))
    assert resumed is not None
    await run.wait()

    # …and rides the resume's first model request.
    assert run.status is RunStatus.done
    answer = "".join(b.text for b in _bodies(run) if b.type == "answer.delta")
    assert answer == "saw: while you were parked"
    assert "message.injected" in _types(run)


async def test_inbox_enqueue_withdraw_drain_events():
    run = Run(id="r1", kind="chat", owner_id=OWNER, stream=RunStream())
    first = run.enqueue_message("first")
    second = run.enqueue_message("second")

    # Withdraw one before it's consumed; unknown/duplicate withdrawals say no.
    assert run.withdraw_message(first.id)
    assert not run.withdraw_message(first.id)
    assert not run.withdraw_message("nope")

    drained = run.drain_messages()
    assert [m.id for m in drained] == [second.id]
    assert run.pending_messages == []
    assert run.drain_messages() == []  # idempotent when empty

    types = [e.body.type for e in run.stream.replay()]
    assert types == ["message.queued", "message.queued", "message.withdrawn", "message.injected"]


async def test_inbox_edit_rewrites_in_place():
    run = Run(id="r1", kind="chat", owner_id=OWNER, stream=RunStream())
    first = run.enqueue_message("first draft")
    second = run.enqueue_message("second")

    # Edit keeps the message's id and its place in the queue.
    assert run.edit_message(first.id, "final wording")
    assert [(m.id, m.text) for m in run.pending_messages] == [
        (first.id, "final wording"),
        (second.id, "second"),
    ]

    # A withdrawn, drained, or unknown message can no longer be edited.
    assert run.withdraw_message(second.id)
    assert not run.edit_message(second.id, "too late")
    drained = run.drain_messages()
    assert [m.text for m in drained] == ["final wording"]
    assert not run.edit_message(first.id, "too late")
    assert not run.edit_message("nope", "never was")

    bodies = [e.body for e in run.stream.replay()]
    edited = [b for b in bodies if b.type == "message.edited"]
    assert [(b.message_id, b.text) for b in edited] == [(first.id, "final wording")]


async def test_a_held_message_stalls_the_drain_and_releases_in_order():
    """A hold is a pause, not a removal — and it blocks what is behind it on purpose: the
    operator's queue is theirs in the order they wrote it, and delivering the third message
    while the second is still being edited reorders their own conversation."""
    run = Run(id="r1", kind="chat", owner_id=OWNER, stream=RunStream())
    first = run.enqueue_message("first")
    second = run.enqueue_message("second")
    third = run.enqueue_message("third")

    assert run.hold_message(second.id, True)
    # Setting the hold it already has succeeds and says nothing new.
    assert run.hold_message(second.id, True)
    assert not run.hold_message("nope", True)

    # The prefix ahead of the hold goes; the held message and everything behind it stay.
    assert [m.id for m in run.drain_messages()] == [first.id]
    assert [m.id for m in run.pending_messages] == [second.id, third.id]
    # And the stall holds — a second boundary still delivers nothing.
    assert run.drain_messages() == []

    assert run.hold_message(second.id, False)
    assert [m.id for m in run.drain_messages()] == [second.id, third.id]

    bodies = _bodies(run)
    held = [(b.message_id, b.held) for b in bodies if b.type == "message.held"]
    assert held == [(second.id, True), (second.id, False)]
    assert [b.message_id for b in bodies if b.type == "message.injected"] == [
        first.id,
        second.id,
        third.id,
    ]


async def test_a_hold_survives_an_edit_and_an_edit_keeps_the_source():
    """The two mutators rebuild a frozen record, so each has to carry what the other
    owns: an edit that dropped ``source`` would re-label a sub-agent's queued report as
    the operator's own words, and one that dropped the hold would inject the very draft
    the hold exists to protect."""
    run = Run(id="r1", kind="chat", owner_id=OWNER, stream=RunStream())
    report = run.enqueue_message("I found it", source="subagent")

    assert run.hold_message(report.id, True)
    assert run.edit_message(report.id, "I found it, eventually")
    [pending] = run.pending_messages
    assert (pending.text, pending.source, pending.held) == (
        "I found it, eventually",
        "subagent",
        True,
    )
    assert run.drain_messages() == []

    assert run.hold_message(report.id, False)
    [drained] = run.drain_messages()
    assert (drained.text, drained.source) == ("I found it, eventually", "subagent")
    injected = [b for b in _bodies(run) if b.type == "message.injected"]
    assert [b.source for b in injected] == ["subagent"]


def _echo_user_texts():
    """Answer with every user text on the incoming request, so the assertion reads
    straight off the answer what the model was actually shown."""

    async def stream_fn(messages, info):
        texts = [
            p.content
            for p in messages[-1].parts
            if isinstance(p, UserPromptPart) and isinstance(p.content, str)
        ]
        yield "saw: " + "|".join(texts)

    return stream_fn


async def test_steering_on_a_regenerate_never_mutates_the_stored_user_message(tmp_path):
    # Guards the invariant `_inject_queued` and `with_tail_context` both document:
    # never mutate what the store shares. On the regenerate path (`prompt is None`)
    # the library may build the first request by reusing the last history message's
    # own parts list — which the store hands out by reference from its in-memory tree
    # — so appending in place would graft the steering text into the operator's own
    # bubble for every later in-process replay.
    #
    # Note this asserts the outcome, not the aliasing: with the current Pydantic AI
    # internals the two lists are not in fact shared here, so the buggy in-place
    # append also passes. It is kept as a guard against that changing, since the cost
    # of the mutation would be silent corruption of the operator's own text.
    store, _engine = await _fresh_store(tmp_path)
    await store.start()
    conv = await store.create_conversation(OWNER, title="t")

    reg = RunRegistry()
    first = build_chat_orchestrator(
        "the original question",
        model=TestModel(custom_output_text="first answer"),
        categories={},
        store=store,
        conversation_id=conv,
    )
    await reg.submit(kind="chat", owner_id=OWNER, orchestrator=first).wait()

    view = await store.messages_view(conv)
    assert [m.role for m in view] == ["user", "assistant"]
    assert await store.regenerate_point(conv, view[1].id)

    # Regenerate off that same history with a message already queued, so the injection
    # lands on the run's *first* request node — the one the library builds by reusing
    # the last history message's own parts list. `submit` creates the task but does not
    # run it until the next tick, so this enqueue is guaranteed to precede it.
    regen = build_chat_orchestrator(
        None,
        model=FunctionModel(stream_function=_echo_user_texts()),
        categories={},
        store=store,
        conversation_id=conv,
    )
    run = reg.submit(kind="chat", owner_id=OWNER, orchestrator=regen)
    run.enqueue_message("steer the retry")
    await run.wait()

    answer = "".join(b.text for b in _bodies(run) if b.type == "answer.delta")
    assert "steer the retry" in answer  # the model did see it

    # ...but the stored user message is still exactly what was typed. Read off the
    # store's own in-memory tree nodes, which is the object the library aliased —
    # a re-projection from the persisted blob would hide the mutation.
    tree = store._cache[conv]
    originals = [
        p.content
        for node in tree.nodes.values()
        for p in getattr(node.message, "parts", [])
        if isinstance(p, UserPromptPart) and isinstance(p.content, str)
    ]
    assert "the original question" in originals
    assert not any(
        text.startswith("the original question") and text != "the original question"
        for text in originals
    )
    await store.stop()


async def test_edited_message_is_injected_with_new_text():
    # Enqueue then edit inside the tool window — the next model request must
    # carry the edited wording, not the original.
    toolset: FunctionToolset[RunDeps] = FunctionToolset()

    # Declares its class: this toolset is composed here rather than shipped in the
    # catalog, so the name registry has never heard of `t_poke` and would otherwise gate
    # it as the furthest-reaching thing it could be. The steering under test happens at
    # the ordinary level, so the tool has to say it only reads.
    @toolset.tool(metadata={"sensitivity": "read"})
    async def poke(ctx: RunContext[RunDeps]) -> str:
        message = ctx.deps.run.enqueue_message("rough draft")
        assert ctx.deps.run.edit_message(message.id, "polished ask")
        return "poked"

    reg = RunRegistry()
    orch = build_chat_orchestrator(
        "hello",
        model=FunctionModel(stream_function=_call_then_report()),
        categories={"t": toolset},
    )
    run = reg.submit(kind="chat", owner_id=OWNER, orchestrator=orch)
    await run.wait()

    assert run.status is RunStatus.done
    answer = "".join(b.text for b in _bodies(run) if b.type == "answer.delta")
    assert answer == "saw: polished ask"
    types = _types(run)
    queued, edited = types.index("message.queued"), types.index("message.edited")
    assert queued < edited < types.index("message.injected")
