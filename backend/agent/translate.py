"""Translate Pydantic AI's native run into our event protocol (Pillar II).

This is the seam where the *engine* becomes the *product stream*. We drive the
run via ``agent.iter()`` so we can observe each graph node, and turn the
library's events into our domain events:

- a ``ModelRequestNode`` is one **step** (step.started/completed around it);
- text parts → ``answer.delta``, thinking parts → ``thinking.delta`` (the
  reasoning/answer split);
- a ``CallToolsNode`` surfaces tool execution → ``tool.started`` /
  ``tool.completed`` / ``tool.failed`` with full args/results inline.

Step boundaries, document lifecycle, citations, and run metrics are *ours* —
the library doesn't know about them; we emit them here and in the engine.
"""

from __future__ import annotations

import asyncio
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
)

from core.citations import Citable
from core.diagnostics import is_platform_failure
from core.serde import jsonable
from runs import (
    AnswerDelta,
    CitationAdded,
    Run,
    StepCompleted,
    StepStarted,
    ThinkingDelta,
    ToolCompleted,
    ToolFailed,
    ToolImage,
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

#: How much of a source's snippet rides the wire. A citation is a *pointer* — the text
#: that lets the operator recognise the source, not a second copy of it. The full passage
#: is already in the tool result the frame sits beside, so a generous cap here would pay
#: for every snippet twice on a search that returned ten.
SNIPPET_MAX_CHARS = 400


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

    A result declares its own sources by implementing :class:`~core.citations.Citable`;
    anything else (a degraded-capability string, a tool with nothing to cite) yields none —
    this is additive, never load-bearing. Asking the result rather than matching on tool
    names keeps this translator out of the business of knowing which features cite things:
    a new tool that returns a citable result is surfaced the day it lands, and Pillar II
    needn't import a feature's service types to recognize it.

    Folding and the Sources-row numbering are the consumer's concern (repeat sightings of
    one source fold by ``key``, keeping the highest ``engagement``; the row numbers by
    position), so this neither folds nor assigns an index.

    ``retrieved_at`` is stamped here for any producer that did not set one. A tool result
    reaches this function at the moment its call completed, which is within milliseconds
    of when the source was actually read — and a producer that threaded a clock through
    itself only to arrive at the same answer would be a second source for one fact.
    ``at`` is the seam the tests pin that stamping through.
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
    """Land an event a tool or a capability emitted, and say whether it was ours.

    Both node streams call this first, because either can carry one: a tool emits while
    its call runs (``CallToolsNode``), a capability hook emits while the request it
    shaped goes out (``ModelRequestNode``). Everything else falls through to the
    model/tool handlers below, which never see these.
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

    One function for a direct call and for a call a script made, so a web search run from
    inside ``run_code`` cites its pages exactly as one the model called would.
    ``user_content`` is what the tool handed back *for the model* rather than as its
    result — pixels, today. It rides the frame beside the call id, so the warm render
    pairs an image to its call exactly; the cold projection has to recover the same
    pairing positionally (see ``_attach_tool_images``)."""
    _ = run.emit(
        ToolCompleted(
            tool_call_id=tool_call_id,
            name=name,
            result=jsonable(content),
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
    run.answer_started = True  # pins the endpoint past this point (AE-5.3)
    run.step_text_chars += len(text)
    _ = run.emit(AnswerDelta(text=text))


def _thinking(run: Run, text: str) -> None:
    run.step_thinking_chars += len(text)
    _ = run.emit(ThinkingDelta(text=text))


def _settle_step(run: Run) -> None:
    run.step_text_chars = 0
    run.step_thinking_chars = 0


def _on_model_event(event: object, run: Run, mark_first_token: Callable[[], None]) -> None:
    """``mark_first_token`` is called for the first output of **any** kind and only
    counts once (see ``TurnTimer.model_request``).

    Any kind is deliberate, and wider than it first looks. Reasoning counts, because on
    a thinking model the reasoning is what arrives first and timing to the first *answer*
    token would bill the whole thinking pass as latency. **Tool calls count too**, even
    though nothing else in this function acts on them: a response that only calls a tool
    has still finished processing the prompt and started emitting: If it reported no
    first token, its entire duration — prefill included — would land in the "generating"
    side of the throughput calculation that subtracts TTFT from the round-trip, and a
    tool-heavy thread would report a decode rate well under the truth.

    So the mark is driven by the event arriving at all, not by what we do with it."""
    if isinstance(event, PartStartEvent | PartDeltaEvent):
        mark_first_token()
    if isinstance(event, PartStartEvent):
        part = event.part
        if isinstance(part, TextPart) and part.content:
            _answer(run, part.content)
        elif isinstance(part, ThinkingPart) and part.content:
            _thinking(run, part.content)
    elif isinstance(event, PartDeltaEvent):
        delta = event.delta
        if isinstance(delta, TextPartDelta) and delta.content_delta:
            _answer(run, delta.content_delta)
        elif isinstance(delta, ThinkingPartDelta) and delta.content_delta:
            _thinking(run, delta.content_delta)
    # PartEndEvent / FinalResultEvent carry no domain signal we surface — tool
    # execution is reported from the CallToolsNode.


def on_tool_event(
    event: object, run: Run, announced: set[str] | None, loop_breaker: LoopBreaker | None
) -> None:
    if isinstance(event, FunctionToolCallEvent):
        part = event.part
        # tool.started is idempotent per run: an approval-deferred call re-fires
        # its call event on the resume turn, so announce each id once.
        if announced is not None and part.tool_call_id in announced:
            return
        if announced is not None:
            announced.add(part.tool_call_id)
        # No-progress guard: trips before we announce a looping call — and *after* the
        # dedupe above, so the re-fired event of a deferred call isn't counted a second
        # time. Counting it twice would halve the effective repeat threshold for every
        # tool an approval grant auto-approves, since those hops share one LoopBreaker.
        #
        # The guard is shown the call's *question*, without the narration the model wrote
        # to go with it: that sentence is prose, rewritten a little every time, and two
        # spellings of the same reason for the same call are still the same call. Leaving
        # it in the signature would make every repeat look novel and quietly switch the
        # guard off. The frame below carries the whole thing, narration included — the
        # operator is the one reader it was written for.
        if loop_breaker is not None:
            loop_breaker.check(
                part.tool_name, strip_narration(part.args_as_dict()), part.tool_call_id
            )
        _ = run.emit(_started_frame(part))
    elif isinstance(event, FunctionToolResultEvent):
        part = event.part
        if loop_breaker is not None:
            # Settle the call with what it answered. A tool that keeps being asked the same
            # question only counts as looping while the answer stops changing, so the guard
            # cannot know whether a call made progress until this event arrives. A failure
            # is an answer like any other: the *same* error three times over is a loop, and
            # one of the commonest.
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

    ``announced`` (a set of tool_call_ids already surfaced as ``tool.started``)
    is threaded across a turn-chain so an approval-deferred call is announced
    once even though its call event re-fires on resume. ``loop_breaker``, if
    given, raises :class:`LoopDetected` to abort a no-progress turn. ``on_step``,
    if given, is called with the accumulated history after each model response —
    the engine uses it to emit a live context/usage frame as the turn progresses.
    ``on_request_node``, if given, is called with each yielded ``ModelRequestNode``
    *before* it streams — the request isn't in flight yet and hasn't been appended
    to the history, so the callback may still amend ``node.request.parts`` (the
    engine injects mid-run operator messages here). ``first_token_timeout_s`` bounds the
    silent wait before each request's first streamed part, raising
    :class:`FirstTokenTimeout`; None leaves that wait to the inactivity watchdog.

    Wall-clock is collected into ``run.timer`` as the nodes are walked: this is the
    only place that sees both node boundaries, so it is where the stopwatch belongs,
    and the run owns it so an approval that splits a turn into several segments still
    accumulates one total.
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
            # Until the first part streams, the server is reading the prompt: silent, and
            # on a local model minutes long for a cold one. The keepalive keeps the
            # inactivity watchdog from reading that as a stall; the first-token bound is
            # what stops a server that never answers.
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
            # The request's non-conversation weight (the brief + the tool schemas) and the
            # brief's named contributors both arrive as capability events at the head of
            # the stream just walked — emitted by `agent/overhead.py` and
            # `agent/injections.py` from the library's own `before_model_request` hook,
            # where the assembled request still exists as parts. `on_chassis_event` has
            # already landed them by the time this line runs.
            if on_step is not None:
                on_step(agent_run.ctx.state.message_history)
        elif Agent.is_call_tools_node(node):
            # Timed as a batch, not per call: the node runs its calls concurrently, so
            # summing them individually would report more tool time than elapsed.
            with timer.tool_calls():
                async with node.stream(agent_run.ctx) as stream:
                    async for event in stream:
                        if not on_chassis_event(event, run):
                            on_tool_event(event, run, announced, loop_breaker)
        # UserPromptNode / End nodes have nothing to stream.
