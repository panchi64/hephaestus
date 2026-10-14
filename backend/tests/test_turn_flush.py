"""``TurnFlush`` — the shared "persist a turn that was stopped from outside" machinery.

The three paths it drives (a bound, a cancel, an unhandled exception) are covered
end-to-end in ``test_persistence.py`` against real orchestrators. What's here is the two
properties the class itself now owns, which both orchestrators depend on and neither can
express alone.
"""

from __future__ import annotations

from collections.abc import Callable

from pydantic_ai import ModelMessage, ModelRequest, UserPromptPart

from agent.flush import CANCELLED_DETAIL, ERRORED_DETAIL, PersistContext, TurnFlush
from agent.history import TurnStart
from runs import Run, RunStatus, RunStream


def _run() -> Run:
    return Run(id="r", kind="chat", owner_id="operator", stream=RunStream())


def _message(text: str) -> ModelRequest:
    return ModelRequest(parts=[UserPromptPart(text)])


type Recorded = list[tuple[list[ModelMessage], str, PersistContext]]


def _cancel(run: Run) -> None:
    assert run.on_cancel is not None
    run.on_cancel()


def _time_out(run: Run) -> None:
    assert run.on_timeout is not None
    run.on_timeout("ran out of time")


def _flush(
    run: Run,
    messages: Callable[[], list[ModelMessage]],
    context: Callable[[], PersistContext],
    recorded: Recorded,
) -> TurnFlush:
    return TurnFlush(
        run,
        messages=messages,
        context=context,
        record=lambda msgs, detail, ctx: recorded.append((msgs, detail, ctx)),
    )


def test_the_context_is_read_when_it_flushes_not_when_it_is_armed():
    # The chat orchestrator arms its hooks in the prelude — before the history is loaded,
    # so before `start` and the prompt's id exist — because a bound can trip during
    # that window and the operator's message must still persist. Capturing the context at
    # arm time would record every such flush against an empty turn.
    run = _run()
    recorded: Recorded = []
    live: dict[str, TurnStart] = {"start": TurnStart()}
    flush = _flush(
        run,
        lambda: [_message("the question")],
        lambda: PersistContext(conversation_id="c", start=live["start"]),
        recorded,
    )
    flush.arm()

    live["start"] = TurnStart(7, 0, "prompt-1")  # the turn got under way after arming
    _cancel(run)

    (_messages, _detail, ctx) = recorded[0]
    assert ctx.start == TurnStart(7, 0, "prompt-1")


def test_a_stop_before_anything_exists_records_nothing():
    run = _run()
    recorded: Recorded = []
    flush = _flush(
        run, list, lambda: PersistContext(conversation_id="c", start=TurnStart()), recorded
    )
    flush.arm()

    _time_out(run)

    assert recorded == []
    assert run.status is not RunStatus.blocked  # nothing to block over
    assert not flush.done  # and the error path is still free to try later


def test_a_recorded_turn_is_not_recorded_again_by_the_error_path():
    run = _run()
    recorded: Recorded = []
    flush = _flush(
        run,
        lambda: [_message("q")],
        lambda: PersistContext(conversation_id="c", start=TurnStart()),
        recorded,
    )
    flush.arm()

    _cancel(run)
    flush.flush_error()  # the task then unwinds

    assert len(recorded) == 1
    assert recorded[0][1] == CANCELLED_DETAIL  # the cancel's marker, not overwritten


def test_disarming_stops_a_late_bound_from_re_recording_a_finished_turn():
    # The hooks fire from outside the orchestrator's task, so they stay reachable through
    # the post-answer window (titling). A bound landing there must not stamp a stop on an
    # answer that completed.
    run = _run()
    recorded: Recorded = []
    flush = _flush(
        run,
        lambda: [_message("q")],
        lambda: PersistContext(conversation_id="c", start=TurnStart()),
        recorded,
    )
    flush.arm()
    flush.disarm()

    assert run.on_timeout is None and run.on_cancel is None


def test_a_bound_blocks_the_run_but_a_cancel_does_not():
    # The registry sets the terminal `cancelled` status itself once the cancellation
    # lands; blocking here would clobber it. A bound has no such owner, so this is where
    # the stop is declared.
    blocked = _run()
    recorded: Recorded = []
    _flush(
        blocked,
        lambda: [_message("q")],
        lambda: PersistContext(conversation_id="c", start=TurnStart()),
        recorded,
    ).arm()
    _time_out(blocked)
    assert blocked.status is RunStatus.blocked
    assert recorded[0][1] == "ran out of time"  # the registry's own words, verbatim

    cancelled = _run()
    recorded2: Recorded = []
    _flush(
        cancelled,
        lambda: [_message("q")],
        lambda: PersistContext(conversation_id="c", start=TurnStart()),
        recorded2,
    ).arm()
    _cancel(cancelled)
    assert cancelled.status is not RunStatus.blocked
    assert recorded2[0][1] == CANCELLED_DETAIL


def test_the_error_path_marks_the_turn_with_its_own_sentence():
    run = _run()
    recorded: Recorded = []
    flush = _flush(
        run,
        lambda: [_message("q")],
        lambda: PersistContext(conversation_id="c", start=TurnStart()),
        recorded,
    )

    flush.flush_error()

    assert recorded[0][1] == ERRORED_DETAIL
    assert run.status is not RunStatus.blocked  # the registry decides an error's outcome
