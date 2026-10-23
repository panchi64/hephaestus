"""Run lifecycle, step, content-delta and limit-notice events."""

from __future__ import annotations

from typing import Literal

from .base import PROTOCOL_VERSION, Body

EndCode = Literal[
    "wall_clock", "inactivity", "first_token", "loop", "context_overflow", "turn_limit"
]


class RunStarted(Body):
    type: Literal["run.started"] = "run.started"
    run_id: str
    kind: str
    protocol_version: int = PROTOCOL_VERSION


class RunQueued(Body):
    """The run is waiting for its model endpoint, at ``position`` in that endpoint's line.
    Sent again whenever the position moves; ``run.started`` ends the wait. Added in v1.1."""

    type: Literal["run.queued"] = "run.queued"
    position: int
    endpoint: str


class RunEnded(Body):
    """The closing frame for a run that reached an expected end.

    A run closes with exactly one of `run.ended` or `run.error`: the payloads differ (a
    failure has a message and exception kind; an expected end an outcome and detail), and
    readers treat either as end-of-stream (`isTerminal` on the client). `error` is
    therefore not an ``outcome``. Both are preceded by `run.metrics`.
    """

    type: Literal["run.ended"] = "run.ended"
    outcome: Literal["done", "blocked", "cancelled"]
    detail: str | None = None
    # Which bound stopped a blocked run, for a client that has to branch on it.
    code: EndCode | None = None


class RunError(Body):
    """The closing frame for a run that failed; the same terminal position as :class:`RunEnded`."""

    type: Literal["run.error"] = "run.error"
    message: str
    kind: str | None = None


class StepStarted(Body):
    type: Literal["step.started"] = "step.started"
    index: int
    title: str | None = None


class StepCompleted(Body):
    type: Literal["step.completed"] = "step.completed"
    index: int


class ThinkingDelta(Body):
    type: Literal["thinking.delta"] = "thinking.delta"
    text: str


class AnswerDelta(Body):
    type: Literal["answer.delta"] = "answer.delta"
    text: str


class AnswerRetracted(Body):
    """The last ``text_chars`` of answer and ``thinking_chars`` of reasoning streamed were
    from a model request that failed and is being sent again; they are not the answer."""

    type: Literal["answer.retracted"] = "answer.retracted"
    text_chars: int
    thinking_chars: int


class LimitNotice(Body):
    type: Literal["limit.notice"] = "limit.notice"
    # "time" | "loop" | "verify" | "context" | "turn_limit" | "continue" (not a stop: a
    # transient model error is being retried). "context" means the context window was
    # exceeded; "turn_limit" that the agent's request cap was reached.
    limit: str
    message: str
    # The stop marker this notice announces when the bound also blocked the turn, the same
    # string persisted as `blocked_reason`. Lets the toast and the stopped turn's marker offer
    # the same remedy: two context stops share one `limit` but not one answer, and a client
    # can't key on prose.
    detail: str | None = None
