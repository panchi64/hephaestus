"""Where a finished turn goes — the one place a turn is written down.

Every way a turn can end converges here: it answered, it hit a bound, it was flushed
from outside (:mod:`agent.flush`), it parked for an approval, or it was cancelled while
still parked. Each of those has its own control flow in the orchestrators, and each of
them would otherwise have needed its own opinion about the persistence boundary, the
verifier's drop range and a turn's attachments — five opinions that could disagree, on
the one operation that is not repeatable.

So the branch that decides *persist now* versus *hand the resume what it needs to persist
later* lives in a single function, and the flush module records through a closure over it
rather than knowing the engine's turn type at all. The park's context is fixed at the
moment it parked (:func:`parked_context`), so a resume's flush and a cancel-while-parked
agree with the park itself about where the turn began.
"""

from __future__ import annotations

from collections.abc import Callable

from pydantic_ai import ModelMessage

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

    Shared by the chat and resume orchestrators so the park/answer-None guards
    are applied *after* the verifier too (a corrective re-attempt can itself park
    or hit a bound). ``context`` is where the turn goes — see :class:`PersistContext`;
    its ``clean_drop`` is a verifier correction's message range to drop from the persisted
    history, and its ``start.prompt_message_id`` names the operator's prompt when the turn
    already wrote it down as it began (see :func:`~agent.prelude.record_prompt`), in which
    case this records only what followed it — the prompt's attachments, references and
    command were stamped on that write."""
    conversation_id = context.conversation_id
    if run.status is RunStatus.awaiting_input:
        # Parked: hand the resume the context to persist the parked turn too.
        if conversation_id is not None and isinstance(run.parked_payload, ParkedTurn):
            run.parked_payload.conversation_id = conversation_id
            run.parked_payload.persist_from = context.start.index
            run.parked_payload.persist_from_parts = context.start.parts
            run.parked_payload.prompt_message_id = context.start.prompt_message_id
            if context.clean_drop is not None:  # re-park: carry the drop range forward
                run.parked_payload.clean_drop = context.clean_drop
        return
    if turn.answer is None and not turn.blocked_reason:
        return  # a bound with nothing to say, or a cancel — the prompt is already on record
    if store is not None and conversation_id is not None:
        messages = turn.messages
        if context.clean_drop is not None:
            reject_idx, nudge_idx = context.clean_drop
            messages = messages[:reject_idx] + messages[nudge_idx + 1 :]
        # The drop range is applied to the whole list and the turn is sliced out after, so
        # the indices keep meaning what they did when the verifier took them: the opening
        # request is only set aside once the slice exists.
        recorded = context.start.prompt_message_id
        turn_messages = context.start.slice(messages)
        if recorded is not None:
            turn_messages = after_recorded_prompt(turn_messages)
        # `blocked_reason` stamps the turn's branch node so a reload shows the same
        # persistent stop marker the live stream rendered. With nothing after the prompt
        # (a stop before the first token) the prompt itself carries it.
        _ = store.record(
            conversation_id,
            split_injected_requests(turn_messages),
            prompt_id=recorded,
            blocked_reason=turn.blocked_reason,
            # The run's stopwatch, one entry per response it streamed, in the order the
            # store will meet them. Recorded on the same call as the messages so a
            # response and its duration can never be persisted apart.
            timings=run.timer.responses,
        )


def flush_recorder(
    run: Run, store: ConversationStore | None
) -> Callable[[list[ModelMessage], str, PersistContext], None]:
    """The closure :class:`TurnFlush` records through — ``finalize`` with this run's
    store already bound, so the flush module never has to know the engine's turn type."""

    def record(messages: list[ModelMessage], detail: str, context: PersistContext) -> None:
        finalize(
            run,
            TurnResult(answer=None, messages=messages, blocked_reason=detail),
            store=store,
            context=context,
        )

    return record


def persist_parked_cancel(run: Run, *, store: ConversationStore | None) -> None:
    """Persist a parked turn's own messages when the operator cancels it while it is
    still awaiting an approval decision, instead of its resume-only persistence
    silently dropping the whole turn (the operator's own prompt included) — the
    parked counterpart of ``_on_cancel``'s flush for a still-running turn. Wired as
    ``run.on_park_cancel`` right after the parking ``finalize`` call populates
    ``ParkedTurn``'s persistence context, so it's armed before any further ``await``
    a concurrent cancel could otherwise slip through (see ``park_for_input``'s
    identical notify-before-park ordering concern). Called by
    ``RunRegistry.cancel``'s parked branch *after* it has already set the terminal
    ``cancelled`` status, so ``finalize`` takes its normal persist branch rather
    than its still-parked one."""
    parked = run.parked_payload
    if not isinstance(parked, ParkedTurn):
        return
    flush_recorder(run, store)(parked.message_history, CANCELLED_DETAIL, parked_context(parked))


def parked_context(parked: ParkedTurn, turn_start: TurnStart | None = None) -> PersistContext:
    """Where a parked turn goes — fixed at the moment it parked, so every path that
    persists it later (a resume's flush hooks, a cancel while still parked) agrees.

    ``turn_start`` is the live boundary a *running* resume holds: a fold during that resume
    rebuilds the history in front of the turn and moves it. A cancel of a still-parked run
    passes nothing, because nothing has folded — the payload's own boundary is the answer."""
    return PersistContext(
        conversation_id=parked.conversation_id,
        start=parked_start(parked) if turn_start is None else turn_start,
        clean_drop=parked.clean_drop,
    )


def parked_start(parked: ParkedTurn) -> TurnStart:
    """The boundary a parked turn parked with — where it begins, and which stored node is
    its already-recorded prompt."""
    return TurnStart(parked.persist_from, parked.persist_from_parts, parked.prompt_message_id)
