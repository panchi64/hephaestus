"""Where a finished turn goes: the one place a turn is written down.

Every ending converges here: answered, hit a bound, flushed from outside
(:mod:`agent.flush`), parked for an approval, or cancelled while parked. Each would
otherwise hold its own opinion on the persistence boundary, the verifier's drop range and
attachments, on the one operation that isn't repeatable.

The branch between persisting now and handing the resume what it needs lives in one
function, and the flush module records through a closure over it without knowing the
engine's turn type. The park's context is fixed when it parks (:func:`parked_context`), so
a resume's flush and a cancel-while-parked agree with the park on where the turn began.
"""

from __future__ import annotations

from collections.abc import Callable

from pydantic_ai import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    ToolCallPart,
    ToolReturnPart,
)

from runs import Run, RunStatus
from services.conversations import ConversationStore

from .flush import CANCELLED_DETAIL, PersistContext
from .history import TurnStart, after_recorded_prompt, split_injected_requests
from .parking import ParkedTurn
from .turn import TurnResult


def finalize(
    run: Run,
    turn: TurnResult,
    *,
    store: ConversationStore | None,
    context: PersistContext,
) -> None:
    """Close out a turn: persist it, or wire resume context if it parked.

    Shared by both orchestrators so the park/answer-None guards also apply after the
    verifier (a correction can itself park or hit a bound). ``context`` (see
    :class:`PersistContext`) carries ``clean_drop``, a verifier correction's range to drop,
    and ``start.prompt_message_id``, the operator's prompt when already written as the turn
    began (:func:`~agent.prelude.record_prompt`), in which case only what followed is
    recorded; the prompt's attachments, references and command were stamped on that write."""
    conversation_id = context.conversation_id
    if run.status is RunStatus.awaiting_input:
        if conversation_id is not None and isinstance(run.parked_payload, ParkedTurn):
            run.parked_payload.conversation_id = conversation_id
            run.parked_payload.persist_from = context.start.index
            run.parked_payload.persist_from_parts = context.start.parts
            run.parked_payload.prompt_message_id = context.start.prompt_message_id
            if context.clean_drop is not None:  # re-park: carry the drop range forward
                run.parked_payload.clean_drop = context.clean_drop
        return
    if turn.answer is None and not turn.blocked_reason:
        return  # a bound with nothing to say, or a cancel; the prompt is already on record
    if store is not None and conversation_id is not None:
        messages = turn.messages
        if context.clean_drop is not None:
            reject_idx, nudge_idx = context.clean_drop
            messages = messages[:reject_idx] + messages[nudge_idx + 1 :]
        # Drop first, slice after, so the verifier's indices keep their meaning; the
        # opening request is set aside only once the slice exists.
        recorded = context.start.prompt_message_id
        turn_messages = context.start.slice(messages)
        if recorded is not None:
            turn_messages = after_recorded_prompt(turn_messages)
        # `blocked_reason` stamps the branch node so a reload shows the live stop marker; with
        # nothing after the prompt (a stop before the first token) the prompt carries it.
        _ = store.record(
            conversation_id,
            split_injected_requests(turn_messages),
            prompt_id=recorded,
            blocked_reason=turn.blocked_reason,
            # One entry per streamed response, recorded with the messages so a response and
            # its duration can't be persisted apart.
            timings=run.timer.responses,
        )


def flush_recorder(
    run: Run, store: ConversationStore | None
) -> Callable[[list[ModelMessage], str, PersistContext], None]:
    """The closure :class:`TurnFlush` records through: ``finalize`` with this run's store bound."""

    def record(messages: list[ModelMessage], detail: str, context: PersistContext) -> None:
        finalize(
            run,
            TurnResult(answer=None, messages=messages, blocked_reason=detail),
            store=store,
            context=context,
        )

    return record


def persist_parked_cancel(run: Run, *, store: ConversationStore | None) -> None:
    """Persist a parked turn's messages when the operator cancels it mid-approval; otherwise
    its resume-only persistence drops the turn and prompt. The parked counterpart of the
    running-turn cancel flush. Wired as ``run.on_park_cancel`` right after the parking
    ``finalize`` populates the persistence context, before any ``await`` a concurrent cancel
    could slip through (cf. ``park_for_input``'s notify-before-park ordering). Called by
    ``RunRegistry.cancel``'s parked branch after the status is already ``cancelled``, so
    ``finalize`` takes its persist branch."""
    parked = run.parked_payload
    if not isinstance(parked, ParkedTurn):
        return
    detail = run.detail if run.status is RunStatus.blocked and run.detail else CANCELLED_DETAIL
    history = [*parked.message_history, *_denied_returns(parked, detail)]
    flush_recorder(run, store)(history, detail, parked_context(parked))


def stamp_parked_block(run: Run, *, store: ConversationStore | None) -> None:
    """Leave the reason a parked run was blocked on its prompt, without writing its turn.
    For a run whose thread already has another run extending it."""
    parked = run.parked_payload
    if store is None or not isinstance(parked, ParkedTurn) or not run.detail:
        return
    if parked.conversation_id is not None and parked.prompt_message_id is not None:
        store.stamp_block(parked.conversation_id, parked.prompt_message_id, run.detail)


def _denied_returns(parked: ParkedTurn, reason: str) -> list[ModelMessage]:
    """A request answering every call of the turn's last response that has no return with a
    denial, so the stored history has no tool call without one."""
    history = parked.message_history
    last = next(
        (i for i in reversed(range(len(history))) if isinstance(history[i], ModelResponse)), None
    )
    if last is None:
        return []
    answered = {
        part.tool_call_id
        for message in history[last + 1 :]
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, ToolReturnPart | RetryPromptPart)
    }
    response = history[last]
    assert isinstance(response, ModelResponse)
    calls = [
        part
        for part in response.parts
        if isinstance(part, ToolCallPart) and part.tool_call_id not in answered
    ]
    if not calls:
        return []
    return [
        ModelRequest(
            parts=[
                ToolReturnPart(
                    tool_name=call.tool_name,
                    content=reason,
                    tool_call_id=call.tool_call_id,
                    outcome="denied",
                )
                for call in calls
            ]
        )
    ]


def parked_context(parked: ParkedTurn, turn_start: TurnStart | None = None) -> PersistContext:
    """Where a parked turn goes, fixed when it parked so every later persisting path agrees.

    ``turn_start`` is the live boundary a running resume holds, which a fold moves. A cancel
    of a still-parked run passes none: nothing has folded, so the payload's is right."""
    return PersistContext(
        conversation_id=parked.conversation_id,
        start=parked_start(parked) if turn_start is None else turn_start,
        clean_drop=parked.clean_drop,
    )


def parked_start(parked: ParkedTurn) -> TurnStart:
    """The boundary a parked turn parked with: where it begins and its recorded prompt node."""
    return TurnStart(parked.persist_from, parked.persist_from_parts, parked.prompt_message_id)
