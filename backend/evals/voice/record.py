"""What a voice run recorded, and the timeline builder that turns a run's events into it."""

from __future__ import annotations

import json
from typing import Annotated, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

from runs.events.approvals import ApprovalRequired, QuestionAsked
from runs.events.lifecycle import AnswerDelta, AnswerRetracted, RunEnded, RunError
from runs.events.plan import TasksUpdated
from runs.events.tools import ToolCompleted, ToolFailed, ToolStarted
from runs.events.union import EventBody

#: How much of a tool's arguments or result the document shows.
_EXCERPT = 400


class _Model(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")


class Text(_Model):
    kind: Literal["text"] = "text"
    text: str


class Tool(_Model):
    kind: Literal["tool"] = "tool"
    tool_call_id: str
    name: str
    narration: str | None = None
    args: str = ""
    status: Literal["running", "ok", "failed"] = "running"
    outcome: str = ""
    #: Made from inside a ``run_code`` script rather than by the model directly.
    nested: bool = False


class Tasks(_Model):
    kind: Literal["tasks"] = "tasks"
    items: list[tuple[str, str]]


class Question(_Model):
    kind: Literal["question"] = "question"
    questions: list[str]
    options: list[list[str]]


class Approval(_Model):
    kind: Literal["approval"] = "approval"
    name: str
    summary: str
    explanation: str | None = None


class Error(_Model):
    kind: Literal["error"] = "error"
    message: str


type Segment = Annotated[
    Text | Tool | Tasks | Question | Approval | Error, Field(discriminator="kind")
]


class Turn(_Model):
    prompt: str
    segments: list[Segment] = Field(default_factory=list)
    #: The run's terminal outcome, ``error``, or ``timeout`` when no terminal event came.
    ended: str = "timeout"
    detail: str | None = None
    wall_s: float = 0.0

    def _reply_start(self) -> int:
        """Where the trailing run of text segments begins."""
        start = len(self.segments)
        while start > 0 and isinstance(self.segments[start - 1], Text):
            start -= 1
        return start

    def work(self) -> list[Segment]:
        """Everything before the final reply: beats, tool cards, instruments."""
        return self.segments[: self._reply_start()]

    def final_reply(self) -> str:
        """The text after the turn's last non-text segment: what the operator reads last."""
        tail = self.segments[self._reply_start() :]
        return "".join(s.text for s in tail if isinstance(s, Text)).strip()


class SubagentRecord(_Model):
    handle: str
    callsign: str
    agent_class: str
    #: The brief Talos wrote for it.
    task: str
    status: str
    report: str | None = None
    findings: object = None
    error: str | None = None


class Sample(_Model):
    scenario: str
    sample: int
    conversation_id: str | None = None
    turns: list[Turn] = Field(default_factory=list)
    title: str | None = None
    #: The live title failed and this one came from the retitle route, which sends the
    #: same title prompt every operator turn rather than the first.
    title_regenerated: bool = False
    work_summary: str | None = None
    subagents: list[SubagentRecord] = Field(default_factory=list)
    #: Why the sample is incomplete, when it is: setup failed, a turn timed out.
    problem: str | None = None


class Run(_Model):
    group: str
    #: The blind name, ``A``, ``B``…; the model behind it is in the group's ``key.json``.
    name: str
    started_at: str
    prompt_fingerprint: str
    stub: bool
    samples: list[Sample] = Field(default_factory=list)


def _excerpt(value: object) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= _EXCERPT else text[: _EXCERPT - 1] + "…"


class Timeline:
    """Builds one turn's segments from its event bodies, in order."""

    def __init__(self, turn: Turn) -> None:
        self.turn: Turn = turn
        self._tools: dict[str, Tool] = {}

    def feed(self, body: EventBody) -> None:
        segments = self.turn.segments
        match body:
            case AnswerDelta(text=text):
                if segments and isinstance(last := segments[-1], Text):
                    last.text += text
                else:
                    segments.append(Text(text=text))
            case AnswerRetracted(text_chars=chars):
                self._retract(chars)
            case ToolStarted():
                args = {k: v for k, v in body.args.items() if k != "narration"}
                narration = body.args.get("narration")
                tool = Tool(
                    tool_call_id=body.tool_call_id,
                    name=body.name,
                    narration=narration if isinstance(narration, str) else None,
                    args=_excerpt(args) if args else "",
                    nested=body.parent_tool_call_id is not None,
                )
                self._tools[body.tool_call_id] = tool
                segments.append(tool)
            case ToolCompleted():
                if tool := self._tools.get(body.tool_call_id):
                    tool.status = "ok"
                    tool.outcome = _excerpt(body.result) if body.result is not None else ""
            case ToolFailed():
                if tool := self._tools.get(body.tool_call_id):
                    tool.status = "failed"
                    tool.outcome = _excerpt(body.error)
            case TasksUpdated():
                segments.append(
                    Tasks(items=[(item["status"], item["content"]) for item in body.items])
                )
            case QuestionAsked():
                segments.append(
                    Question(
                        questions=[q.question for q in body.questions],
                        options=[[o.label for o in q.options] for q in body.questions],
                    )
                )
            case ApprovalRequired():
                segments.append(
                    Approval(name=body.name, summary=body.summary, explanation=body.explanation)
                )
            case RunEnded():
                self.turn.ended = body.outcome
                self.turn.detail = body.detail
            case RunError():
                self.turn.ended = "error"
                self.turn.detail = f"{body.kind}: {body.message}"
                segments.append(Error(message=body.message))
            case _:
                pass

    def _retract(self, chars: int) -> None:
        """Drop the last ``chars`` of answer text: it came from a request being resent."""
        segments = self.turn.segments
        index = len(segments) - 1
        while chars > 0 and index >= 0:
            segment = segments[index]
            if isinstance(segment, Text):
                cut = min(chars, len(segment.text))
                segment.text = segment.text[: len(segment.text) - cut]
                chars -= cut
                if not segment.text:
                    del segments[index]
            index -= 1
