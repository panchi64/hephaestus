"""The second look at a finished answer, and the one correction it is allowed.

A completed turn goes to a judge (injected, or the utility model) and a failed verdict
buys exactly one more pass: a synthetic nudge carrying the reason, re-driven through
:func:`~agent.turn.drive_turn`, with the rejected answer and nudge marked for removal so the
persisted thread reads original request → corrected answer.

The bounds make this its own module. The correction is a second full pass over a just-
finished history, so it runs at peak pressure and cannot fold (its drop range holds
absolute pre-fold indices). Every way it ends (no room, a park, a bound mid-correction)
must carry the drop range where the persist will find it, or an answer nobody wrote persists.
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
    """Judge only turns that made a tool call (a checkable artifact); heuristic off ⇒ all."""
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
    max_requests: int | None = None,
    ledger_prompt_message_id: str | None = None,
) -> TurnResult:
    """Judge the answer; on failure make a single bounded corrective re-attempt.

    A passing answer returns unchanged. Otherwise the correction's full history is returned
    with a ``clean_drop`` range (rejected answer plus nudge) that ``finalize`` removes on
    persist. If the correction parks, the range rides on the parked payload so the resume
    cleans too; if it hits a bound it is returned as-is, with no lost answer.

    ``turn_start`` is the first pass's boundary: the correction is part of that turn, so a
    call it defers is reviewed at Auto against the operator's opening request read off it
    (``agent/gating.py``), not the tail window alone.
    """
    if not turn.answer or not turn.answer.strip():
        return turn
    verdict = await judge(prompt, turn.answer)
    if verdict.ok:
        return turn
    nudge = VERIFIER_NUDGE.format(reason=verdict.reason)
    # Peak pressure and no folding (see the module docstring): against a window with no room
    # a correction buys a context stop in place of a real answer. Keep the answer, say what
    # was skipped.
    if no_room_for(run, turn.messages, nudge, threshold=context_threshold, settings=settings):
        _ = run.emit(LimitNotice(limit="verify", message="skipped: no room for a re-attempt"))
        return turn
    _ = run.emit(LimitNotice(limit="verify", message=f"re-attempting: {verdict.reason}"))
    # The rejected response (last of the original attempt) and the injected nudge (first of
    # the correction): two adjacent messages.
    clean_drop = (len(turn.messages) - 1, len(turn.messages))
    # Published before re-driving so a stop during the correction drops the same two
    # messages; otherwise it persists the rejected answer and a message nobody typed, and
    # both replay to the model on every later turn.
    if drop_ref is not None:
        drop_ref[:] = [clean_drop]
    # One attempt only, with no re-verify.
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
        binding=turn.binding or binding,
        vision=vision,
        partial_history_ref=partial_history_ref,
        store=store,
        turn_start=turn_start,
        # No fold under a correction: `clean_drop` indexes the pre-fold history.
        correcting=True,
        # The same turn continuing: same workspace, and it spends what the first pass left.
        workspace_key=workspace_key,
        reported=turn.reported,
        max_requests=max_requests,
        requests_used=turn.requests_used,
        ledger_prompt_message_id=ledger_prompt_message_id,
    )
    if run.status is RunStatus.awaiting_input:
        # The resume's persist must drop the rejected answer and nudge too.
        if isinstance(run.parked_payload, ParkedTurn):
            run.parked_payload.clean_drop = clean_drop
        return corrected
    if corrected.answer is None:
        # A bound: the range must ride along or the rejected answer and nudge persist as transcript.
        return TurnResult(
            answer=None,
            messages=corrected.messages,
            clean_drop=clean_drop,
            blocked_reason=corrected.blocked_reason,
        )
    return TurnResult(
        answer=corrected.answer,
        messages=corrected.messages,
        clean_drop=clean_drop,
        binding=corrected.binding,
    )
