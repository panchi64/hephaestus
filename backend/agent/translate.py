"""Translate Pydantic AI's native run into our event protocol.

The run is driven via ``agent.iter()`` and each graph node becomes domain events:

- a ``ModelRequestNode`` is one step (step.started/completed around it);
- text parts → ``answer.delta``, thinking parts → ``thinking.delta``;
- a ``CallToolsNode`` → ``tool.started`` / ``tool.completed`` / ``tool.failed`` with full
  args and results inline;
- a tool call's streaming arguments → throttled ``tool.preparing`` frames.

Step boundaries, citations and run metrics are ours; the library doesn't know them.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from contextlib import AsyncExitStack
from datetime import UTC, datetime

from pydantic_ai import (
    Agent,
    AgentRun,
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    ModelMessage,
    ModelRequestNode,
    PartDeltaEvent,
    PartStartEvent,
    RetryPromptPart,
    TextPart,
    TextPartDelta,
    ThinkingPart,
    ThinkingPartDelta,
    ToolCallPart,
    ToolCallPartDelta,
)

from core.citations import Citable
from core.diagnostics import is_platform_failure
from core.envelopes import result_parts
from core.serde import jsonable
from runs import (
    AnswerDelta,
    CitationAdded,
    PreparingCall,
    Run,
    StepCompleted,
    StepStarted,
    ThinkingDelta,
    ToolCompleted,
    ToolFailed,
    ToolImage,
    ToolPreparing,
    ToolStarted,
)
from services.conversation_view import tool_images
from tools.emit import RunEventEmitted
from tools.narration import strip_narration

from .emit import (
    ChassisEvent,
    NestedToolFinished,
    NestedToolStarted,
    OverheadMeasured,
    PrefixWatched,
)
from .meta import FirstTokenTimeout, LoopBreaker

#: Snippet length on the wire. A citation is a pointer for recognising the source; the full
#: passage is already in the adjacent tool result, so a generous cap would pay twice.
SNIPPET_MAX_CHARS = 400

PREPARING_STEP_CHARS = 256


def _clip(text: str | None) -> str | None:
    if text is None:
        return None
    folded = " ".join(text.split())
    if len(folded) <= SNIPPET_MAX_CHARS:
        return folded or None
    return folded[: SNIPPET_MAX_CHARS - 1] + "…"


def citations_from_tool_result(
    content: object, *, at: datetime | None = None
) -> list[CitationAdded]:
    """Sources a completed tool call surfaced, in the order the result gave them.

    A result declares its sources by implementing :class:`~core.citations.Citable`; anything
    else yields none (additive, never load-bearing). Asking the result rather than matching
    tool names keeps this translator ignorant of which features cite, so a new citable tool
    surfaces without importing its service types.

    Folding and Sources-row numbering belong to the consumer (repeat sightings fold by
    ``key`` keeping the highest ``engagement``; rows number by position), so this neither
    folds nor indexes.

    ``retrieved_at`` is stamped here for producers that set none: the result arrives within
    milliseconds of the read. ``at`` lets tests pin the stamp.
    """
    if not isinstance(content, Citable):
        return []
    stamp = at or datetime.now(UTC)
    return [
        CitationAdded(
            key=c.key,
            url=c.url,
            title=c.title,
            kind=c.kind,
            engagement=c.engagement,
            snippet=_clip(c.snippet),
            published=c.published,
            retrieved_at=c.retrieved_at or stamp,
            source_id=c.source_id,
            ref=c.ref,
        )
        for c in content.citations()
    ]


def on_chassis_event(event: object, run: Run) -> bool:
    """Land an event a tool or capability emitted, and say whether it was ours.

    Both node streams call this first: a tool emits while its call runs
    (``CallToolsNode``), a capability hook while its request goes out
    (``ModelRequestNode``). Everything else falls through to the handlers below.
    """
    if isinstance(event, RunEventEmitted | ChassisEvent):
        _ = run.emit(event.body)
        return True
    if isinstance(event, OverheadMeasured):
        run.context_overhead = event.overhead
        return True
    if isinstance(event, PrefixWatched):
        run.prefix_verdict = event.verdict
        return True
    if isinstance(event, NestedToolStarted):
        _ = run.emit(_started_frame(event.part, parent=event.parent_tool_call_id))
        return True
    if isinstance(event, NestedToolFinished):
        call_id, name = event.tool_call_id or "", event.tool_name or ""
        if event.error is not None:
            _ = run.emit(
                ToolFailed(
                    tool_call_id=call_id,
                    name=name,
                    error=event.error,
                    parent_tool_call_id=event.parent_tool_call_id,
                    platform=is_platform_failure(event.error),
                )
            )
        else:
            _emit_result(
                run,
                call_id,
                name,
                event.content,
                event.user_content,
                parent=event.parent_tool_call_id,
            )
        return True
    return False


def _started_frame(part: ToolCallPart, *, parent: str | None = None) -> ToolStarted:
    """The ``tool.started`` frame for one call — a direct one, or one a script made."""
    return ToolStarted(
        tool_call_id=part.tool_call_id,
        name=part.tool_name,
        args=part.args_as_dict(),
        parent_tool_call_id=parent,
    )


def _emit_result(
    run: Run,
    tool_call_id: str,
    name: str,
    content: object,
    user_content: object,
    *,
    parent: str | None = None,
) -> None:
    """``tool.completed`` for a call that returned, then the sources it surfaced.

    Shared by direct calls and `run_code` script calls, so both cite pages alike.
    ``user_content`` is what the tool handed back for the model rather than as its result
    (pixels, today); it rides the frame beside the call id so the warm render pairs image to
    call exactly, while the cold projection recovers the pairing positionally
    (``_attach_tool_images``)."""
    result = jsonable(content)
    _ = run.emit(
        ToolCompleted(
            tool_call_id=tool_call_id,
            name=name,
            result=result,
            result_parts=result_parts(result),
            images=[
                ToolImage(media_type=image.media_type, data=image.data)
                for image in tool_images(user_content)
            ],
            parent_tool_call_id=parent,
        )
    )
    for citation in citations_from_tool_result(content):
        _ = run.emit(citation)


def _answer(run: Run, text: str) -> None:
    run.step_text_chars += len(text)
    _ = run.emit(AnswerDelta(text=text))


def _thinking(run: Run, text: str) -> None:
    run.step_thinking_chars += len(text)
    _ = run.emit(ThinkingDelta(text=text))


def _settle_step(run: Run) -> None:
    run.step_text_chars = 0
    run.step_thinking_chars = 0
    run.preparing_calls.clear()


def _announce_preparing(run: Run, call: PreparingCall, replaces: str | None = None) -> None:
    call.emitted_chars = call.chars
    call.announced = True
    _ = run.emit(
        ToolPreparing(
            tool_call_id=call.tool_call_id,
            name=call.name,
            args_chars=call.chars,
            replaces=replaces,
        )
    )


def _args_length(args: str | dict[str, object] | None) -> int:
    if args is None:
        return 0
    return len(args) if isinstance(args, str) else len(json.dumps(args))


def _on_tool_call_start(run: Run, index: int, part: ToolCallPart) -> None:
    call = PreparingCall(part.tool_call_id, part.tool_name, _args_length(part.args))
    run.preparing_calls[index] = call
    if call.name and call.tool_call_id:
        _announce_preparing(run, call)


def _on_tool_call_delta(run: Run, index: int, delta: ToolCallPartDelta) -> None:
    call = run.preparing_calls.get(index)
    if call is None:
        call = PreparingCall(delta.tool_call_id or "", "")
        run.preparing_calls[index] = call
    replaces: str | None = None
    if delta.tool_call_id and delta.tool_call_id != call.tool_call_id:
        if call.announced:
            replaces = call.tool_call_id
        call.tool_call_id = delta.tool_call_id
    call.name += delta.tool_name_delta or ""
    call.chars += _args_length(delta.args_delta)
    # The client keys the card by id, so a frame without one would orphan it.
    if not call.name or not call.tool_call_id:
        return
    if replaces is not None:
        _announce_preparing(run, call, replaces)
    elif not call.announced or call.chars - call.emitted_chars >= PREPARING_STEP_CHARS:
        _announce_preparing(run, call)


def _on_model_event(event: object, run: Run, mark_first_token: Callable[[], None]) -> None:
    """``mark_first_token`` is called for the first output of any kind and counts once
    (``TurnTimer.model_request``).

    Reasoning counts because on a thinking model it arrives first, and timing to the first
    answer token would bill the whole thinking pass as latency. Tool calls count too: a
    response that only calls a tool has finished prefill, and without a mark its whole
    duration would land in the generating side of the throughput calculation, understating
    the decode rate of tool-heavy threads. The mark is driven by any event arriving."""
    if isinstance(event, PartStartEvent | PartDeltaEvent):
        mark_first_token()
    if isinstance(event, PartStartEvent):
        part = event.part
        if isinstance(part, TextPart) and part.content:
            _answer(run, part.content)
        elif isinstance(part, ThinkingPart) and part.content:
            _thinking(run, part.content)
        elif isinstance(part, ToolCallPart):
            _on_tool_call_start(run, event.index, part)
    elif isinstance(event, PartDeltaEvent):
        delta = event.delta
        if isinstance(delta, TextPartDelta) and delta.content_delta:
            _answer(run, delta.content_delta)
        elif isinstance(delta, ThinkingPartDelta) and delta.content_delta:
            _thinking(run, delta.content_delta)
        elif isinstance(delta, ToolCallPartDelta):
            _on_tool_call_delta(run, event.index, delta)


def on_tool_event(
    event: object, run: Run, announced: set[str] | None, loop_breaker: LoopBreaker | None
) -> None:
    if isinstance(event, FunctionToolCallEvent):
        part = event.part
        # Idempotent per run: an approval-deferred call re-fires its call event on resume.
        if announced is not None and part.tool_call_id in announced:
            return
        if announced is not None:
            announced.add(part.tool_call_id)
        # No-progress guard, after the dedupe above so a re-fired deferred call isn't counted
        # twice (which would halve the threshold for grant-approved tools, whose hops share
        # one LoopBreaker).
        #
        # Shown the call without its narration: that prose is reworded each time, so keeping
        # it in the signature would make every repeat look novel and disable the guard. The
        # frame below carries the narration for the operator.
        if loop_breaker is not None:
            loop_breaker.check(
                part.tool_name, strip_narration(part.args_as_dict()), part.tool_call_id
            )
        _ = run.emit(_started_frame(part))
    elif isinstance(event, FunctionToolResultEvent):
        part = event.part
        if loop_breaker is not None:
            # A repeated call loops only while its answer stops changing, so progress is
            # unknown until this event. A failure is an answer too: the same error three
            # times is a loop, and one of the commonest.
            answer = part.model_response() if isinstance(part, RetryPromptPart) else part.content
            loop_breaker.observe(part.tool_call_id, answer)
        if isinstance(part, RetryPromptPart):
            _ = run.emit(
                ToolFailed(
                    tool_call_id=part.tool_call_id,
                    name=part.tool_name or "",
                    error=part.model_response(),
                )
            )
        elif part.outcome == "failed":
            error = str(part.content)
            _ = run.emit(
                ToolFailed(
                    tool_call_id=part.tool_call_id,
                    name=part.tool_name,
                    error=error,
                    platform=is_platform_failure(error),
                )
            )
        else:
            _emit_result(
                run,
                part.tool_call_id,
                part.tool_name,
                part.content,
                getattr(event, "content", None),
            )


async def stream_agent_run[DepsT](
    agent_run: AgentRun[DepsT, object],
    run: Run,
    *,
    announced: set[str] | None = None,
    loop_breaker: LoopBreaker | None = None,
    on_step: Callable[[list[ModelMessage]], None] | None = None,
    on_request_node: Callable[[ModelRequestNode[DepsT, object]], None] | None = None,
    first_token_timeout_s: float | None = None,
) -> None:
    """Iterate the AgentRun's graph nodes, emitting our events as they happen.

    ``announced`` (tool_call_ids already surfaced as ``tool.started``) is threaded across a
    turn-chain so an approval-deferred call is announced once though its event re-fires on
    resume. ``loop_breaker`` raises :class:`LoopDetected` to abort a no-progress turn.
    ``on_step`` receives the accumulated history after each model response (the engine
    emits a live context/usage frame from it). ``on_request_node`` receives each
    ``ModelRequestNode`` before it streams, while ``node.request.parts`` can still be
    amended (the engine injects mid-run operator messages). ``first_token_timeout_s``
    bounds the silent wait before a request's first part, raising
    :class:`FirstTokenTimeout`; None leaves it to the inactivity watchdog.

    Wall-clock is collected into ``run.timer`` here, the only place seeing both node
    boundaries; the run owns it so a turn split by approvals accumulates one total.
    """
    step = 0
    timer = run.timer
    async for node in agent_run:
        if Agent.is_model_request_node(node):
            if on_request_node is not None:
                on_request_node(node)
            step += 1
            _settle_step(run)
            _ = run.emit(StepStarted(index=step))
            # Until the first part streams the server is reading the prompt, minutes on a
            # cold local model. The keepalive stops the watchdog reading that as a stall;
            # the first-token bound stops a server that never answers.
            waiting = AsyncExitStack()
            if first_token_timeout_s is not None:
                await waiting.enter_async_context(run.keepalive())
            deadline = asyncio.timeout(first_token_timeout_s)
            try:
                async with deadline:
                    with timer.model_request() as mark_first_token:
                        async with node.stream(agent_run.ctx) as stream:
                            async for event in stream:
                                if on_chassis_event(event, run):
                                    continue
                                if deadline.when() is not None:
                                    deadline.reschedule(None)
                                    await waiting.aclose()
                                _on_model_event(event, run, mark_first_token)
            except TimeoutError as exc:
                if deadline.expired() and first_token_timeout_s is not None:
                    raise FirstTokenTimeout(first_token_timeout_s) from exc
                raise
            finally:
                await waiting.aclose()
            _settle_step(run)
            _ = run.emit(StepCompleted(index=step))
            # The request's overhead and the brief's contributors arrive as capability events
            # at the head of the stream (`agent/overhead.py`, `agent/injections.py`, from
            # `before_model_request`); `on_chassis_event` has landed them by here.
            if on_step is not None:
                on_step(agent_run.ctx.state.message_history)
        elif Agent.is_call_tools_node(node):
            # Timed as a batch: calls run concurrently, so summing would exceed elapsed time.
            with timer.tool_calls():
                async with node.stream(agent_run.ctx) as stream:
                    async for event in stream:
                        if not on_chassis_event(event, run):
                            on_tool_event(event, run, announced, loop_breaker)
        # UserPromptNode / End nodes have nothing to stream.
