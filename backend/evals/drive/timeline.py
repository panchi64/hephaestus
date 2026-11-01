"""What a turn showed on its stream, and the timeline builder that turns its events into it."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

from runs.events.approvals import ApprovalRequired, QuestionAsked
from runs.events.lifecycle import (
    AnswerDelta,
    AnswerRetracted,
    LimitNotice,
    RunEnded,
    RunError,
    StepStarted,
)
from runs.events.messages import MessageInjected
from runs.events.plan import PermissionChanged, PlanUpdated, TasksUpdated
from runs.events.tools import ToolCompleted, ToolFailed, ToolStarted
from runs.events.union import EventBody

from .client import Policy, policy_refusal

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
    #: The model request it was made in, from 1; calls sharing one ran in parallel.
    step: int = 0
    #: The failure was the platform's fault, not the call's.
    platform: bool = False


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
    #: The note the eval refused it with; None when it was approved.
    refused: str | None = None
    #: An excerpt of the call's arguments.
    args: str = ""


class Error(_Model):
    kind: Literal["error"] = "error"
    message: str


class Injected(_Model):
    """A message sent while the turn ran reached the model."""

    kind: Literal["injected"] = "injected"
    message_id: str
    source: str


class Plan(_Model):
    kind: Literal["plan"] = "plan"
    title: str
    status: str
    steps: list[str]


class Permission(_Model):
    kind: Literal["permission"] = "permission"
    level: str
    reason: str


class Limit(_Model):
    kind: Literal["limit"] = "limit"
    limit: str
    message: str


_NOTICES = Injected | Plan | Permission | Limit

type Segment = Annotated[
    Text | Tool | Tasks | Question | Approval | Error | Injected | Plan | Permission | Limit,
    Field(discriminator="kind"),
]


class Turn(_Model):
    prompt: str
    #: A run the inbox started to hand the agent a sub-agent's report, not an operator turn.
    delivery: bool = False
    run_id: str = ""
    segments: list[Segment] = Field(default_factory=list)
    #: The run's terminal outcome, ``error``, or ``timeout`` when no terminal event came.
    ended: str = "timeout"
    detail: str | None = None
    wall_s: float = 0.0
    #: Model requests the run made.
    steps: int = 0

    def _reply_start(self) -> int:
        """Where the trailing run of text segments begins; a notice arriving among or
        after them (a limit, a level change) doesn't end the reply."""
        start = len(self.segments)
        while start > 0 and isinstance(self.segments[start - 1], Text | _NOTICES):
            start -= 1
        return start

    def work(self) -> list[Segment]:
        """Everything but the final reply: beats, tool cards, instruments, notices."""
        start = self._reply_start()
        tail = [s for s in self.segments[start:] if not isinstance(s, Text)]
        return [*self.segments[:start], *tail]

    def final_reply(self) -> str:
        """The text after the turn's last non-text segment: what the operator reads last."""
        tail = self.segments[self._reply_start() :]
        return "".join(s.text for s in tail if isinstance(s, Text)).strip()

    def tools(self) -> list[Tool]:
        return [s for s in self.segments if isinstance(s, Tool)]


class SubagentRecord(_Model):
    handle: str
    callsign: str
    agent_class: str
    #: The brief the launching agent wrote for it.
    task: str
    status: str
    report: str | None = None
    findings: object = None
    error: str | None = None
    #: The roster name it was launched from, and its own thread.
    name: str = ""
    conversation_id: str = ""
    parent_conversation_id: str = ""
    depth: int = 1
    #: ``<endpoint name>/<model>`` and what chose it: ``pin``, ``spawn``, ``role``, ``parent``.
    model: str = ""
    model_source: str = ""
    #: Its own runs, recorded from their streams after it ended.
    turns: list[Turn] = Field(default_factory=list)


def redact_home(text: str) -> str:
    """``text`` with the home directory, which names the operator's account, as ``~``."""
    return text.replace(str(Path.home()), "~")


def _excerpt(value: object) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= _EXCERPT else text[: _EXCERPT - 1] + "…"


class Timeline:
    """Builds one turn's segments from its event bodies, in order."""

    def __init__(self, turn: Turn, policy: Policy | None = None) -> None:
        self.turn: Turn = turn
        self._policy: Policy = policy or Policy()
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
            case StepStarted():
                self.turn.steps += 1
            case ToolStarted():
                args = {k: v for k, v in body.args.items() if k != "narration"}
                narration = body.args.get("narration")
                tool = Tool(
                    tool_call_id=body.tool_call_id,
                    name=body.name,
                    narration=narration if isinstance(narration, str) else None,
                    args=_excerpt(args) if args else "",
                    nested=body.parent_tool_call_id is not None,
                    step=self.turn.steps,
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
                    tool.platform = body.platform
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
                    Approval(
                        name=body.name,
                        summary=body.summary,
                        explanation=body.explanation,
                        refused=policy_refusal(body, self._policy),
                        args=_excerpt(body.args) if body.args else "",
                    )
                )
            case MessageInjected():
                segments.append(Injected(message_id=body.message_id, source=body.source))
            case PlanUpdated():
                segments.append(Plan(title=body.title, status=body.status, steps=body.steps))
            case PermissionChanged():
                segments.append(Permission(level=body.level, reason=body.reason))
            case LimitNotice():
                segments.append(Limit(limit=body.limit, message=body.message))
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
