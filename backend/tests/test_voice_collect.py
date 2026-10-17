"""The voice timeline: a run's events become beats, cards, instruments and a final reply."""

from __future__ import annotations

from evals.voice.document import render
from evals.voice.record import Approval, Question, Run, Sample, Tasks, Text, Timeline, Tool, Turn
from evals.voice.spec import Scenario
from runs.events.approvals import ApprovalRequired, QuestionAsked, QuestionOption, QuestionSpec
from runs.events.lifecycle import AnswerDelta, AnswerRetracted, RunEnded
from runs.events.plan import TasksUpdated
from runs.events.tools import ToolCompleted, ToolFailed, ToolStarted
from runs.events.union import EventBody


def _turn(*bodies: EventBody) -> Turn:
    turn = Turn(prompt="fix the tests")
    timeline = Timeline(turn)
    for body in bodies:
        timeline.feed(body)
    return turn


def test_text_between_tools_is_a_beat_and_the_tail_is_the_reply() -> None:
    turn = _turn(
        AnswerDelta(text="Running the "),
        AnswerDelta(text="suite first."),
        ToolStarted(
            tool_call_id="t1",
            name="shell",
            args={"command": "pytest", "narration": "Runs the suite to see what fails."},
        ),
        ToolCompleted(tool_call_id="t1", name="shell", result="2 failed"),
        AnswerDelta(text="Found it. "),
        AnswerDelta(text="Green now."),
        RunEnded(outcome="done"),
    )
    assert [type(s) for s in turn.work()] == [Text, Tool]
    assert turn.final_reply() == "Found it. Green now."
    tool = turn.segments[1]
    assert isinstance(tool, Tool)
    assert tool.narration == "Runs the suite to see what fails."
    assert "narration" not in tool.args
    assert (tool.status, tool.outcome) == ("ok", "2 failed")
    assert turn.ended == "done"


def test_a_retraction_trims_the_resent_text() -> None:
    turn = _turn(
        AnswerDelta(text="Kept. "),
        AnswerDelta(text="Dropped"),
        AnswerRetracted(text_chars=7, thinking_chars=0),
        AnswerDelta(text="Resent."),
    )
    assert turn.final_reply() == "Kept. Resent."


def test_a_retraction_longer_than_the_last_segment_reaches_back() -> None:
    turn = _turn(AnswerDelta(text="abc"), AnswerRetracted(text_chars=10, thinking_chars=0))
    assert turn.segments == []


def test_instruments_are_recorded_in_order() -> None:
    turn = _turn(
        TasksUpdated(
            items=[{"id": "1", "content": "Read", "status": "in_progress", "active_form": None}]
        ),
        QuestionAsked(
            tool_call_id="q",
            questions=[
                QuestionSpec(
                    question="Delete or compress?",
                    options=[QuestionOption(label="Compress"), QuestionOption(label="Delete")],
                )
            ],
        ),
        ApprovalRequired(tool_call_id="a", name="shell", summary="Deletes old logs."),
        ToolStarted(tool_call_id="t", name="shell"),
        ToolFailed(tool_call_id="t", name="shell", error="exit 1"),
    )
    tasks, question, approval, tool = turn.segments
    assert isinstance(tasks, Tasks) and tasks.items == [("in_progress", "Read")]
    assert isinstance(question, Question) and question.options == [["Compress", "Delete"]]
    assert isinstance(approval, Approval) and approval.summary == "Deletes old logs."
    assert isinstance(tool, Tool) and (tool.status, tool.outcome) == ("failed", "exit 1")
    assert turn.final_reply() == ""


def test_the_document_is_blind_and_carries_lint() -> None:
    scenario = Scenario.model_validate(
        {
            "id": "one-liner",
            "voice": "talos",
            "about": "A one-line answer.",
            "prompts": ["429?"],
            "dimensions": ["shape"],
            "expect": ["Answers 429 (§5)."],
        }
    )
    turn = _turn(AnswerDelta(text="Great question! It's 429."), RunEnded(outcome="done"))
    run = Run(
        group="g",
        name="A",
        started_at="now",
        prompt_fingerprint="abc",
        stub=False,
        samples=[
            Sample(scenario="one-liner", sample=1, turns=[turn], title="HTTP rate limit code")
        ],
    )
    text = render(run, [scenario])
    assert "# Voice run g / A" in text
    assert "> Great question! It's 429." in text
    assert "`stock-opener`: Great question" in text
    assert "Dimensions: `shape`, `helper`" in text
    assert "- title: 'HTTP rate limit code'" in text
    assert "stub" not in text
