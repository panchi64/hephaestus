"""The translation layer: Pydantic AI's node/event stream → our events."""

from __future__ import annotations

from pydantic_ai import (
    Agent,
    FunctionToolResultEvent,
    ModelMessage,
    ModelResponse,
    RunContext,
    ToolReturnPart,
)
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel
from pydantic_ai.models.test import TestModel

from agent import stream_agent_run
from agent.injections import AnnounceInjections
from agent.overhead import MeasureOverhead
from agent.translate import on_tool_event
from runs import (
    Run,
    RunStream,
    ToolCompleted,
    ToolFailed,
    ToolPreparing,
    ToolStarted,
    ViewLiveStopped,
)
from tools.emit import RunEventEmitted

from ._helpers import bodies_of, first_body
from ._json import dump, n, obj


def _run() -> Run:
    return Run(id="t", kind="chat", owner_id="operator", stream=RunStream())


def _bodies(run: Run):
    return [e.body for e in run.stream.replay()]


async def test_translates_steps_text_and_tool_calls():
    agent = Agent(TestModel(custom_output_text="all done"))

    @agent.tool_plain
    def add(a: int, b: int) -> int:
        return a + b

    run = _run()
    async with agent.iter("add two numbers") as agent_run:
        await stream_agent_run(agent_run, run)

    types = [b.type for b in _bodies(run)]
    assert "step.started" in types and "step.completed" in types
    assert "tool.started" in types and "tool.completed" in types
    assert "answer.delta" in types

    started = first_body(run, ToolStarted)
    assert started.name == "add"
    assert set(started.args) == {"a", "b"}

    completed = first_body(run, ToolCompleted)
    args = obj(dump(started)["args"])
    assert dump(completed)["result"] == n(args["a"]) + n(args["b"])

    answer = "".join(b.text for b in _bodies(run) if b.type == "answer.delta")
    assert answer == "all done"


async def test_a_retried_call_becomes_tool_failed():
    run = _run()
    event = FunctionToolResultEvent(
        part=ToolReturnPart(
            tool_name="lookup", content="bad input", tool_call_id="c9", outcome="retried"
        )
    )
    on_tool_event(event, run, None, None)

    failed = first_body(run, ToolFailed)
    assert failed.name == "lookup"
    assert failed.tool_call_id == "c9"
    assert "bad input" in failed.error


async def test_a_parallel_batch_streams_every_call_independently():
    # Both tools requested in ONE assistant message, run concurrently. What must survive
    # is per-call identity: the transcript upserts by tool_call_id, so a batch that
    # blurred into one event would collapse into one card or misfile a result.
    agent = Agent(TestModel(custom_output_text="done"))

    @agent.tool_plain
    def alpha(q: str) -> str:
        return f"a:{q}"

    @agent.tool_plain
    def beta(q: str) -> str:
        return f"b:{q}"

    run = _run()
    async with agent.iter("use both") as agent_run:
        await stream_agent_run(agent_run, run)

    bodies = _bodies(run)
    started = [b for b in bodies if b.type == "tool.started"]
    completed = [b for b in bodies if b.type == "tool.completed"]

    assert {b.name for b in started} == {"alpha", "beta"}
    assert len({b.tool_call_id for b in started}) == 2
    # Both go out before either comes back — the shape a serial-only mapping can't make.
    order = [b.type for b in bodies if b.type in ("tool.started", "tool.completed")]
    assert order == ["tool.started", "tool.started", "tool.completed", "tool.completed"]
    # Each result is filed under the call that asked for it, not merged or swapped.
    assert {b.tool_call_id for b in completed} == {b.tool_call_id for b in started}

    # And the batch is ONE model round-trip: a second step only opens for the answer.
    assert len([b for b in bodies if b.type == "step.started"]) == 2


# ── What a tool and a capability say for themselves ──────────────────────────────
#
# Both used to reach through `ctx.deps` for the `Run` and call `run.emit`. They now
# emit into the library's own event stream and this translator lands them, which is
# what these cover: that the seam carries them at all, that a capability needs nothing
# of ours to use it, and that a tool's news arrives inside its own call rather than
# racing it.


async def test_a_tool_puts_its_own_news_on_the_stream():
    agent = Agent(TestModel(custom_output_text="done"))

    @agent.tool
    async def announce(ctx: RunContext[object], note: str) -> str:
        _ = await ctx.emit(RunEventEmitted(body=ViewLiveStopped(conversation_id=note)))
        return "ok"

    run = _run()
    async with agent.iter("say something") as agent_run:
        await stream_agent_run(agent_run, run)

    stopped = first_body(run, ViewLiveStopped)
    assert stopped.conversation_id
    # Inside its own call, not before it: the ordering the direct `run.emit` could not
    # promise, because a value written straight to the run is visible immediately while
    # the frame for the call it belongs to waits for the translator to reach it.
    order = [
        b.type
        for b in _bodies(run)
        if b.type.startswith(("tool.", "view.")) and b.type != "tool.preparing"
    ]
    assert order == ["tool.started", "view.live.stopped", "tool.completed"]


async def test_the_capabilities_need_nothing_of_ours_to_report():
    """Neither capability touches `ctx.deps` any more, so both work on an agent that has
    none — which is the whole point of moving them onto the library's event seam, and the
    thing a mock of that seam would never catch."""

    def repo_instructions() -> str:
        return "CLAUDE.md says be brief."

    agent = Agent(
        TestModel(custom_output_text="done"),
        capabilities=[MeasureOverhead(), AnnounceInjections()],
    )
    _ = agent.instructions(name="repo")(repo_instructions)

    run = _run()
    async with agent.iter("go") as agent_run:
        await stream_agent_run(agent_run, run)

    # The brief's named contributor was announced, once, with its text.
    injected = [b for b in _bodies(run) if b.type == "context.injected"]
    assert [(b.contributor, b.text) for b in injected] == [("repo", "CLAUDE.md says be brief.")]
    # And the request's non-conversation weight reached the run, where the gauge reads it.
    assert run.context_overhead is not None
    assert run.context_overhead.system >= len("CLAUDE.md says be brief.")
    assert any(block.id == "repo" for block in run.context_overhead.blocks)


async def test_a_scripts_calls_carry_the_script_that_made_them():
    """A call a `run_code` script made reaches the stream as the ordinary `tool.*` frames,
    each naming the script's own call — the one field a client nests the row by."""
    from pydantic_ai.messages import ToolCallPart

    from agent.emit import NestedToolFinished, NestedToolStarted
    from agent.translate import on_chassis_event

    run = _run()
    part = ToolCallPart(tool_name="web_search", args={"query": "q"}, tool_call_id="s__1")
    assert on_chassis_event(NestedToolStarted(parent_tool_call_id="s", part=part), run)
    assert on_chassis_event(
        NestedToolFinished(
            parent_tool_call_id="s",
            content={"hits": 1},
            tool_call_id="s__1",
            tool_name="web_search",
        ),
        run,
    )
    assert on_chassis_event(
        NestedToolFinished(
            parent_tool_call_id="s", error="nope", tool_call_id="s__2", tool_name="files_read_file"
        ),
        run,
    )

    started, completed, failed = _bodies(run)
    assert isinstance(started, ToolStarted)
    assert isinstance(completed, ToolCompleted)
    assert isinstance(failed, ToolFailed)
    assert (started.type, started.tool_call_id, started.args) == (
        "tool.started",
        "s__1",
        {"query": "q"},
    )
    assert (completed.type, dump(completed)["result"]) == ("tool.completed", {"hits": 1})
    assert (failed.type, failed.error) == ("tool.failed", "nope")
    assert {b.parent_tool_call_id for b in (started, completed, failed)} == {"s"}


async def test_a_direct_calls_frames_name_no_parent():
    agent = Agent(TestModel(custom_output_text="ok"))

    @agent.tool_plain
    def add(a: int, b: int) -> int:
        return a + b

    run = _run()
    async with agent.iter("add") as agent_run:
        await stream_agent_run(agent_run, run)
    frames = [b for b in _bodies(run) if isinstance(b, (ToolStarted, ToolCompleted, ToolFailed))]
    assert frames and all(b.parent_tool_call_id is None for b in frames)


async def _preparing_run(*pieces: DeltaToolCall) -> Run:
    async def stream(messages: list[ModelMessage], _info: AgentInfo):
        if any(isinstance(m, ModelResponse) for m in messages):
            yield "done"
            return
        yield {0: pieces[0]}
        for piece in pieces[1:]:
            yield {0: piece}

    agent = Agent(FunctionModel(stream_function=stream))

    @agent.tool_plain
    def add(a: int = 0) -> int:
        return a

    run = _run()
    async with agent.iter("go") as agent_run:
        await stream_agent_run(agent_run, run)
    return run


async def test_a_named_tool_call_announces_itself_once_then_throttles():
    run = await _preparing_run(
        DeltaToolCall(name="add", json_args='{"a": 1', tool_call_id="c1"),
        DeltaToolCall(json_args=" " * 100),
        DeltaToolCall(json_args=" " * 300),
        DeltaToolCall(json_args="}"),
    )
    frames = bodies_of(run, ToolPreparing)
    assert [(f.tool_call_id, f.name, f.args_chars) for f in frames] == [
        ("c1", "add", 7),
        ("c1", "add", 407),
    ]
    assert run.preparing_calls == {}


async def test_a_call_id_changed_after_announce_is_reported_at_once():
    run = await _preparing_run(
        DeltaToolCall(name="add", json_args="{", tool_call_id="old"),
        DeltaToolCall(json_args=" ", tool_call_id="new"),
    )
    frames = bodies_of(run, ToolPreparing)
    assert [(f.tool_call_id, f.replaces) for f in frames] == [("old", None), ("new", "old")]


async def test_an_unnamed_start_announces_when_the_name_arrives():
    run = await _preparing_run(
        DeltaToolCall(json_args="{", tool_call_id="c2"),
        DeltaToolCall(name="add", json_args='"a": 1}'),
    )
    frames = bodies_of(run, ToolPreparing)
    assert [(f.tool_call_id, f.name) for f in frames] == [("c2", "add")]
