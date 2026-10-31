"""The chassis tail stays attached to the prompt it was sent with through merges, slices
and a regenerate's replacement, and no reader takes it for the operator's words."""

from __future__ import annotations

from dataclasses import replace

from pydantic_ai import InstructionBaselinePart, ModelMessage, ModelRequest, UserPromptPart

from agent.history import (
    TurnStart,
    merge_consecutive_requests,
    split_recorded_opening,
    with_tail_context,
)
from agent.title import last_user_text
from agent.work_summary import work_transcript
from services.chassis_tail import operator_content, tail_stamp

_CHECKPOINT = UserPromptPart(content="[checkpoint] earlier work, summarized")


def _prompt(*tail: str) -> ModelRequest:
    part = UserPromptPart(content=["fix the parser", *tail])
    return ModelRequest(parts=[part], metadata=tail_stamp([part], len(tail)))


def _user_parts(request: ModelRequest) -> list[UserPromptPart]:
    return [p for p in request.parts if isinstance(p, UserPromptPart)]


def test_a_merge_behind_a_checkpoint_keeps_the_tail_on_the_prompt():
    merged = merge_consecutive_requests(
        [ModelRequest(parts=[_CHECKPOINT]), _prompt("<tasks>open: 1</tasks>")]
    )
    assert len(merged) == 1
    request = merged[0]
    assert isinstance(request, ModelRequest)
    checkpoint, prompt = _user_parts(request)
    assert operator_content(request, checkpoint) == checkpoint.content
    assert operator_content(request, prompt) == ["fix the parser"]
    assert last_user_text(merged) == "[checkpoint] earlier work, summarized fix the parser"


def test_a_sliced_boundary_keeps_the_tail_on_the_prompt():
    merged = merge_consecutive_requests(
        [ModelRequest(parts=[_CHECKPOINT]), _prompt("<tasks>open: 1</tasks>")]
    )
    (request,) = TurnStart(index=0, history_parts=1).slice(merged)
    assert isinstance(request, ModelRequest)
    (prompt,) = _user_parts(request)
    assert operator_content(request, prompt) == ["fix the parser"]


def test_parts_the_library_appends_after_a_fold_stay_with_the_turn():
    merged = merge_consecutive_requests([ModelRequest(parts=[_CHECKPOINT]), _prompt()])
    request = merged[0]
    assert isinstance(request, ModelRequest)
    baseline = InstructionBaselinePart(instructions=[])
    sent: list[ModelMessage] = [replace(request, parts=[*request.parts, baseline])]
    opening, rest = split_recorded_opening(TurnStart(index=0, history_parts=1).slice(sent))
    assert opening is not None and opening.parts[1:] == [baseline] and rest == []


def test_a_regenerate_replaces_the_old_tail_behind_a_checkpoint():
    history = merge_consecutive_requests(
        [ModelRequest(parts=[_CHECKPOINT]), _prompt("STOP NOTICE", "<tasks>open: 1</tasks>")]
    )
    (request,) = with_tail_context(history, ["<tasks>open: 2</tasks>"])
    assert isinstance(request, ModelRequest)
    checkpoint, prompt = _user_parts(request)
    assert checkpoint.content == _CHECKPOINT.content
    assert prompt.content == ["fix the parser", "<tasks>open: 2</tasks>"]
    assert operator_content(request, prompt) == ["fix the parser"]


def test_a_regenerate_with_no_fresh_context_still_drops_the_old_tail():
    (request,) = with_tail_context([_prompt("STOP NOTICE")], [])
    assert isinstance(request, ModelRequest)
    (prompt,) = _user_parts(request)
    assert prompt.content == ["fix the parser"]


def test_the_work_summary_shows_only_the_operators_words():
    transcript = work_transcript([_prompt("<tasks>open: 1</tasks>")])
    assert "fix the parser" in transcript
    assert "open: 1" not in transcript
