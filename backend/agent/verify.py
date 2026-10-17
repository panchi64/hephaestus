"""The second look at a finished answer, and the one correction it is allowed.

A turn that produced an answer is not necessarily a turn that produced a *good* one, and
the model that wrote it is the last thing that should be asked. So a completed turn goes
to a judge — an injected one, or the utility model standing in — and a failed verdict
buys exactly one more pass: a synthetic nudge carrying the reason, re-driven through
:func:`~agent.turn.drive_turn`, with the rejected answer and the nudge marked for removal
so the persisted thread reads original request → corrected answer.

The bounds are the whole reason this is its own module rather than a branch in the
orchestrator. The correction is a *second* full pass over a history that has just finished
its first, so it runs at the turn's peak pressure and it cannot fold (its drop range is a
pair of absolute indices into the pre-fold history). Every way that can end — no room to
try, a park for approval, a bound mid-correction — has to carry the drop range somewhere
the persist will find it, and getting one of them wrong persists an answer nobody wrote.
"""

from __future__ import annotations

from collections.abc import Callable

from pydantic_ai import ModelMessage

from core.config import Settings
from core.container import ServiceContainer
from prompts.agent import VERIFIER_NUDGE
from runs import LimitNotice, Run, RunStatus
from services.conversations import ConversationBinding, ConversationStore
from tools import NO_DISABLED_TOOLS

from .factory import TurnAgent
from .history import TurnStart
from .meta import Judge
from .metrics import no_room_for
from .parking import DEFAULT_BINDING, ParkedTurn
from .turn import NO_CAPS, TurnResult, drive_turn


def should_verify(settings: Settings, run: Run) -> bool:
    """The verifier's heuristic trigger: judge only turns that produced a
    checkable artifact (made a tool call). Off ⇒ judge every answer."""
    if not settings.verify_heuristic:
        return True
    return bool(run.metrics and run.metrics.tool_calls)


async def verify_and_correct(
    run: Run,
    agent: TurnAgent,
    prompt: str,
    turn: TurnResult,
    announced: set[str],
    judge: Judge,
    *,
    settings: Settings,
    caps: ServiceContainer = NO_CAPS,
    conversation_id: str | None = None,
    disabled_tools: frozenset[str] = NO_DISABLED_TOOLS,
    partial_history_ref: list[Callable[[], list[ModelMessage]]] | None = None,
    store: ConversationStore | None = None,
    binding: ConversationBinding = DEFAULT_BINDING,
    vision: bool = True,
    drop_ref: list[tuple[int, int]] | None = None,
    context_threshold: float | None = None,
    turn_start: TurnStart | None = None,
    workspace_key: str = "",
) -> TurnResult:
    """Judge the answer; on failure make a single bounded corrective re-attempt.

    A passing answer returns unchanged. Otherwise the correction's full history
    is returned with a ``clean_drop`` range that ``finalize`` removes on persist
    (the rejected answer + the synthetic nudge), so the recorded history reads
    original request → corrected answer. If the correction itself parks for
    approval, the drop range rides on the parked payload so the resume cleans too;
    if it hits a bound, it is returned as-is (no premature persist, no lost answer).

    ``turn_start`` is the same boundary the first pass ran under, threaded because the
    correction is *part of that turn* rather than a turn of its own: a call the re-attempt
    defers is reviewed at the Auto level like any other, and the review's opening line is
    the operator's request read off this boundary (``agent/gating.py``). Without it the
    correction reviewed against the tail window alone — at the one moment a turn is
    guaranteed to have run far past its own opening request.
    """
    if not turn.answer or not turn.answer.strip():
        return turn  # nothing checkable to verify
    verdict = await judge(prompt, turn.answer)
    if verdict.ok:
        return turn
    nudge = VERIFIER_NUDGE.format(reason=verdict.reason)
    # The correction is a *second* full pass over a history that has just finished its
    # first, so it runs at the turn's peak pressure — and it cannot fold, because its drop
    # range is a pair of absolute indices into this history. Attempting it against a
    # window that has no room left buys a context stop in place of an answer that, however
    # the judge scored it, is a real answer. Keep the answer and say what was skipped.
    if no_room_for(run, turn.messages, nudge, threshold=context_threshold, settings=settings):
        _ = run.emit(LimitNotice(limit="verify", message="skipped: no room for a re-attempt"))
        return turn
    _ = run.emit(LimitNotice(limit="verify", message=f"re-attempting: {verdict.reason}"))
    # The range to drop on persist: the rejected ModelResponse (last message of
    # the original attempt) through the injected nudge ModelRequest (the first
    # new message of the correction) — two adjacent messages.
    clean_drop = (len(turn.messages) - 1, len(turn.messages))
    # Publish the range before re-driving, so a stop *during* the correction (an
    # inactivity bound, the operator's Stop) drops the same two messages the completed
    # path does. Without it, a stop here persists the rejected answer and a user message
    # nobody typed, and both replay to the model on every later turn.
    if drop_ref is not None:
        drop_ref[:] = [clean_drop]
    # One attempt only — no re-verify, so it cannot retry endlessly.
    corrected = await drive_turn(
        run,
        agent,
        settings=settings,
        prompt=nudge,
        message_history=turn.messages,
        announced=announced,
        caps=caps,
        conversation_id=conversation_id,
        disabled_tools=disabled_tools,
        binding=binding,
        vision=vision,
        partial_history_ref=partial_history_ref,
        store=store,
        turn_start=turn_start,
        # No fold under a correction: `clean_drop` indexes the pre-fold history.
        correcting=True,
        # The correction is this turn continuing, so it works where the turn worked.
        workspace_key=workspace_key,
    )
    if run.status is RunStatus.awaiting_input:
        # The correction needs approval: carry the drop range on the parked turn
        # so the resume's persist drops the rejected answer + nudge as well.
        if isinstance(run.parked_payload, ParkedTurn):
            run.parked_payload.clean_drop = clean_drop
        return corrected
    if corrected.answer is None:
        # Hit a bound — the caller finalizes it, but the drop range has to ride along or
        # the rejected answer and the synthetic nudge persist as real transcript.
        return TurnResult(
            answer=None,
            messages=corrected.messages,
            clean_drop=clean_drop,
            blocked_reason=corrected.blocked_reason,
        )
    return TurnResult(answer=corrected.answer, messages=corrected.messages, clean_drop=clean_drop)
