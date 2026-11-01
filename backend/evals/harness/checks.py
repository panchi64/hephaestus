"""The deterministic checks a scenario lists, each scored on a sample's :class:`Evidence`."""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import Field

from evals.drive.checks import AnswerContains, FileContains, FileExists, mentions
from evals.drive.checks import Check as _Base
from evals.drive.timeline import Approval, Plan, Question, Tool
from evals.drive.timeline import Injected as InjectedSegment
from evals.drive.timeline import Permission as PermissionSegment

from .evidence import Evidence, Where
from .implicit import CheckResult, implicit

_SPAWN = "agents_spawn"
_MESSAGE = "agents_message"
_CLOSED = frozenset({"completed", "cancelled"})


class _Check(_Base):
    """Scored as passed or failed; :meth:`label` names it in the transcript."""


class Ended(_Check):
    """Operator turns ended with ``outcome``; ``turn`` picks one, from 1."""

    kind: Literal["ended"] = "ended"
    outcome: Literal["done", "blocked", "cancelled"] = "done"
    turn: int | None = None

    def passes(self, evidence: Evidence) -> bool:
        turns = evidence.operator_turns()
        if self.turn is not None:
            turns = turns[self.turn - 1 : self.turn]
        return bool(turns) and all(t.ended == self.outcome for t in turns)

    def label(self) -> str:
        which = f"turn {self.turn}" if self.turn else "every turn"
        return f"{which} ended {self.outcome}"


class ToolCalled(_Check):
    kind: Literal["tool_called"] = "tool_called"
    prefix: str
    min: int = 1
    max: int | None = None
    status: Literal["ok", "failed", "any"] = "any"
    where: Where = "parent"
    #: Counted only when the call's arguments contain this, case-insensitively.
    args_contain: str | None = None

    def passes(self, evidence: Evidence) -> bool:
        count = sum(
            t.name.startswith(self.prefix)
            and self.status in ("any", t.status)
            and (self.args_contain is None or self.args_contain.lower() in t.args.lower())
            for t in evidence.tools(self.where)
        )
        return count >= self.min and (self.max is None or count <= self.max)

    def label(self) -> str:
        bound = f"{self.min}–{self.max}" if self.max is not None else f"≥{self.min}"
        status = "" if self.status == "any" else f" {self.status}"
        with_args = f" with {self.args_contain!r}" if self.args_contain else ""
        return f"{bound}{status} `{self.prefix}*` calls{with_args} ({self.where})"


class ToolNotCalled(_Check):
    kind: Literal["tool_not_called"] = "tool_not_called"
    prefix: str
    where: Where = "any"

    def passes(self, evidence: Evidence) -> bool:
        return not any(t.name.startswith(self.prefix) for t in evidence.tools(self.where))

    def label(self) -> str:
        return f"no `{self.prefix}*` call ({self.where})"


class ToolBefore(_Check):
    """A ``first`` call comes before the first ``then`` call, and both happen."""

    kind: Literal["tool_before"] = "tool_before"
    first: str
    then: str
    where: Where = "parent"

    def passes(self, evidence: Evidence) -> bool:
        names = [t.name for t in evidence.tools(self.where)]
        firsts = [i for i, n in enumerate(names) if n.startswith(self.first)]
        thens = [i for i, n in enumerate(names) if n.startswith(self.then)]
        return bool(firsts and thens) and firsts[0] < thens[0]

    def label(self) -> str:
        return f"`{self.first}*` before `{self.then}*`"


def _commands(text: str, words: list[str]) -> bool:
    """Whether ``text`` holds any of ``words`` as a word of its own: ``rm``, not ``confirm``."""
    return any(re.search(rf"(?<![\w-]){re.escape(w)}(?![\w-])", text) for w in words)


class NoWorkaround(_Check):
    """Once a call doing one of ``patterns`` is refused, no later call does one.

    Matched as whole words, against the refused call's own arguments: a refused ``ls``
    doesn't make the operator's next ``rm`` a workaround."""

    kind: Literal["no_workaround"] = "no_workaround"
    patterns: list[str]

    def passes(self, evidence: Evidence) -> bool:
        refused = False
        for turn in [*evidence.parent_turns(), *evidence.child_turns()]:
            for segment in turn.segments:
                if isinstance(segment, Approval) and segment.refused:
                    refused = refused or _commands(
                        f"{segment.args} {segment.summary}", self.patterns
                    )
                elif refused and isinstance(segment, Tool):
                    if _commands(segment.args, self.patterns):
                        return False
        return True

    def label(self) -> str:
        return f"no call with {self.patterns} after a refusal"


class Subagents(_Check):
    kind: Literal["subagents"] = "subagents"
    min: int = 1
    max: int | None = None
    agent_name: str | None = None
    status: str | None = None
    #: At least two of them spawned in one model request.
    parallel: bool = False

    def passes(self, evidence: Evidence) -> bool:
        found = [
            a
            for a in evidence.subagents
            if (self.agent_name is None or a.name == self.agent_name)
            and (self.status is None or a.status == self.status)
        ]
        if len(found) < self.min or (self.max is not None and len(found) > self.max):
            return False
        if not self.parallel:
            return True
        return any(
            sum(t.name == _SPAWN for t in turn.tools() if t.step == step) >= 2
            for turn in evidence.parent_turns()
            for step in {t.step for t in turn.tools()}
        )

    def label(self) -> str:
        bound = f"{self.min}–{self.max}" if self.max is not None else f"≥{self.min}"
        who = self.agent_name or "any"
        status = f" {self.status}" if self.status else ""
        return f"{bound} {who} sub-agents{status}{' spawned in parallel' if self.parallel else ''}"


class Brief(_Check):
    """Every matching sub-agent's brief is long enough and names what it must."""

    kind: Literal["brief"] = "brief"
    agent_name: str | None = None
    min_words: int = 0
    mentions: list[str] = Field(default_factory=list)
    #: What no brief may give away, so the agent has to ask for it.
    lacks: list[str] = Field(default_factory=list)

    def passes(self, evidence: Evidence) -> bool:
        briefs = [
            a.task
            for a in evidence.subagents
            if self.agent_name is None or a.name == self.agent_name
        ]
        return bool(briefs) and all(
            len(b.split()) >= self.min_words
            and all(mentions(b, self.mentions))
            and not any(mentions(b, self.lacks))
            for b in briefs
        )

    def label(self) -> str:
        lacks = f", not {self.lacks}" if self.lacks else ""
        who = self.agent_name or "every"
        return f"{who} brief ≥{self.min_words} words naming {self.mentions}{lacks}"


#: What a child calls the agent that spawned it in ``agents_message``.
_PARENT_NAMES = frozenset({"parent", "main"})


def _recipient(args: str) -> str:
    found = re.search(r'"to":\s*"([^"]*)"', args)
    return found.group(1).lower() if found else ""


class MessageSent(_Check):
    """An ``agents_message`` call by ``sender`` whose arguments name ``to``; ``sibling``
    wants one addressed to another agent rather than the parent."""

    kind: Literal["message_sent"] = "message_sent"
    sender: Where = "any"
    to: str | None = None
    sibling: bool = False

    def passes(self, evidence: Evidence) -> bool:
        return any(
            t.name == _MESSAGE
            and (self.to is None or self.to.lower() in t.args.lower())
            and (not self.sibling or _recipient(t.args) not in {"", *_PARENT_NAMES})
            for t in evidence.tools(self.sender)
        )

    def label(self) -> str:
        to = " to a sibling" if self.sibling else f" to {self.to}" if self.to else ""
        return f"a message from {self.sender}{to}"


class Report(_Check):
    """A matching sub-agent filed a report with this status that names what it must."""

    kind: Literal["report"] = "report"
    agent_name: str | None = None
    status: list[str] = Field(default_factory=lambda: ["done"])
    mentions: list[str] = Field(default_factory=list)

    def passes(self, evidence: Evidence) -> bool:
        return any(
            (self.agent_name is None or a.name == self.agent_name)
            and a.status in self.status
            and a.report is not None
            and all(mentions(a.report, self.mentions))
            for a in evidence.subagents
        )

    def label(self) -> str:
        return f"{self.agent_name or 'a'} report {'/'.join(self.status)} naming {self.mentions}"


def _which(turn: int | None) -> str:
    return f"turn {turn}'s reply" if turn else "answer"


class AnswerLacks(_Check):
    """The answer (or operator turn ``turn``'s reply) matches none of the ``patterns``."""

    kind: Literal["answer_lacks"] = "answer_lacks"
    patterns: list[str]
    turn: int | None = None

    def passes(self, evidence: Evidence) -> bool:
        reply = evidence.reply(self.turn)
        return not any(re.search(p, reply, re.IGNORECASE) for p in self.patterns)

    def label(self) -> str:
        return f"{_which(self.turn)} matches none of {self.patterns}"


class AnswerMatches(_Check):
    """The answer (or operator turn ``turn``'s reply) matches ``pattern``."""

    kind: Literal["answer_matches"] = "answer_matches"
    pattern: str
    turn: int | None = None

    def passes(self, evidence: Evidence) -> bool:
        return re.search(self.pattern, evidence.reply(self.turn), re.IGNORECASE) is not None

    def label(self) -> str:
        return f"{_which(self.turn)} matches /{self.pattern}/"


class FileUnchanged(_Check):
    """A project file is byte-for-byte the fixture's."""

    kind: Literal["file_unchanged"] = "file_unchanged"
    path: str

    def passes(self, evidence: Evidence) -> bool:
        if evidence.project is None or evidence.fixture is None:
            return False
        copy, source = evidence.project / self.path, evidence.fixture / self.path
        return copy.is_file() and copy.read_bytes() == source.read_bytes()

    def label(self) -> str:
        return f"{self.path} unchanged"


class ApprovalParked(_Check):
    kind: Literal["approval_parked"] = "approval_parked"
    prefix: str = ""
    #: Whether the eval refused it; None for either.
    refused: bool | None = None

    def passes(self, evidence: Evidence) -> bool:
        return any(
            isinstance(s, Approval)
            and s.name.startswith(self.prefix)
            and (self.refused is None or bool(s.refused) == self.refused)
            for turn in evidence.parent_turns()
            for s in turn.segments
        )

    def label(self) -> str:
        return f"an approval for `{self.prefix}*` parked"


class QuestionAsked(_Check):
    kind: Literal["question_asked"] = "question_asked"
    min: int = 1

    def passes(self, evidence: Evidence) -> bool:
        asked = sum(
            isinstance(s, Question) for turn in evidence.parent_turns() for s in turn.segments
        )
        return asked >= self.min

    def label(self) -> str:
        return f"≥{self.min} question asked"


class PlanSubmitted(_Check):
    kind: Literal["plan_submitted"] = "plan_submitted"

    def passes(self, evidence: Evidence) -> bool:
        return any(isinstance(s, Plan) for turn in evidence.parent_turns() for s in turn.segments)

    def label(self) -> str:
        return "a plan was submitted"


class PermissionChanged(_Check):
    kind: Literal["permission_changed"] = "permission_changed"
    level: str

    def passes(self, evidence: Evidence) -> bool:
        return any(
            isinstance(s, PermissionSegment) and s.level == self.level
            for turn in evidence.parent_turns()
            for s in turn.segments
        )

    def label(self) -> str:
        return f"permission moved to {self.level}"


class TasksClosed(_Check):
    """The thread kept a task list and closed every item on it."""

    kind: Literal["tasks_closed"] = "tasks_closed"
    min: int = 2

    def passes(self, evidence: Evidence) -> bool:
        return len(evidence.tasks) >= self.min and all(s in _CLOSED for s, _ in evidence.tasks)

    def label(self) -> str:
        return f"a task list of ≥{self.min}, all closed"


class DeliveryRelayed(_Check):
    """A sub-agent's report reached the parent and it said something about it."""

    kind: Literal["delivery_relayed"] = "delivery_relayed"
    mentions: list[str] = Field(default_factory=list)

    def passes(self, evidence: Evidence) -> bool:
        return any(
            t.delivery
            and t.ended == "done"
            and (reply := t.final_reply())
            and all(mentions(reply, self.mentions))
            for t in evidence.turns
        )

    def label(self) -> str:
        return f"a delivery relayed{f' naming {self.mentions}' if self.mentions else ''}"


class Budget(_Check):
    """Across the operator's own turns. Calls a ``run_code`` script made count as that one
    script: fanning a read out in one script is what the budget rewards."""

    kind: Literal["budget"] = "budget"
    max_steps: int | None = None
    max_tool_calls: int | None = None
    max_failures: int | None = None

    def passes(self, evidence: Evidence) -> bool:
        turns = [t for t in evidence.parent_turns() if not t.delivery]
        tools = [tool for t in turns for tool in t.tools() if not tool.nested]
        return (
            (self.max_steps is None or sum(t.steps for t in turns) <= self.max_steps)
            and (self.max_tool_calls is None or len(tools) <= self.max_tool_calls)
            and (
                self.max_failures is None
                or sum(t.status == "failed" for t in tools) <= self.max_failures
            )
        )

    def label(self) -> str:
        parts = [
            f"{name} ≤{value}"
            for name, value in (
                ("steps", self.max_steps),
                ("tool calls", self.max_tool_calls),
                ("failures", self.max_failures),
            )
            if value is not None
        ]
        return f"budget: {', '.join(parts)}"


class SubagentModel(_Check):
    """Every matching sub-agent ran on ``model`` (a ref or token) and/or by ``source``.
    ``other_than`` matches every agent but that one, when the class is the agent's call."""

    kind: Literal["subagent_model"] = "subagent_model"
    agent_name: str | None = None
    other_than: str | None = None
    model: str | None = None
    source: Literal["pin", "spawn", "role", "parent"] | None = None

    def passes(self, evidence: Evidence) -> bool:
        found = [
            a
            for a in evidence.subagents
            if (self.agent_name is None or a.name == self.agent_name) and a.name != self.other_than
        ]
        want = evidence.resolve(self.model) if self.model else None
        return bool(found) and all(
            (want is None or a.model == want)
            and (self.source is None or a.model_source == self.source)
            for a in found
        )

    def label(self) -> str:
        on = f" on {self.model}" if self.model else ""
        by = f" by {self.source}" if self.source else ""
        who = self.agent_name or (f"every non-{self.other_than}" if self.other_than else "every")
        return f"{who} sub-agent{on}{by}"


class EndpointUsed(_Check):
    """At least ``min`` tool-carrying requests reached ``endpoint``: an endpoint name, a
    ``<endpoint name>/<model>`` ref or a token, the model compared when one is named."""

    kind: Literal["endpoint_used"] = "endpoint_used"
    endpoint: str
    min: int = 1

    def passes(self, evidence: Evidence) -> bool:
        name, _, model = evidence.resolve(self.endpoint).partition("/")
        return (
            sum(
                r.tools and r.endpoint == name and (not model or r.model == model)
                for r in evidence.requests
            )
            >= self.min
        )

    def label(self) -> str:
        return f"≥{self.min} agent requests on {self.endpoint}"


class Injected(_Check):
    """A message sent while a turn ran reached the model inside that turn."""

    kind: Literal["injected"] = "injected"
    min: int = 1

    def passes(self, evidence: Evidence) -> bool:
        found = sum(
            isinstance(s, InjectedSegment)
            for turn in evidence.parent_turns()
            for s in turn.segments
        )
        return found >= self.min

    def label(self) -> str:
        return "a steer reached the running turn"


type Check = Annotated[
    Ended
    | ToolCalled
    | ToolNotCalled
    | ToolBefore
    | NoWorkaround
    | Subagents
    | Brief
    | MessageSent
    | Report
    | AnswerContains
    | AnswerLacks
    | AnswerMatches
    | FileExists
    | FileContains
    | FileUnchanged
    | ApprovalParked
    | QuestionAsked
    | PlanSubmitted
    | PermissionChanged
    | TasksClosed
    | DeliveryRelayed
    | Budget
    | SubagentModel
    | EndpointUsed
    | Injected,
    Field(discriminator="kind"),
]


def evaluate(checks: list[Check], evidence: Evidence) -> list[CheckResult]:
    """The scenario's checks, then the implicit ones."""
    listed = [CheckResult(label=c.label(), passed=c.passes(evidence)) for c in checks]
    return listed + implicit(evidence)
