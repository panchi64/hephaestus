"""Run lifecycle, step, content-delta and limit-notice events."""

from __future__ import annotations

from typing import Literal

from .base import PROTOCOL_VERSION, _Body


class RunStarted(_Body):
    type: Literal["run.started"] = "run.started"
    run_id: str
    kind: str
    protocol_version: int = PROTOCOL_VERSION


class RunEnded(_Body):
    """The closing frame for a run that reached an *expected* end.

    A run closes with exactly one of `run.ended` or `run.error`, never both — the
    terminal frame has two shapes because the two carry different payloads, and a
    failure has a message and an exception kind where an expected end has an outcome
    and a detail. Readers treat either as end-of-stream (`isTerminal` on the client).
    `error` is deliberately absent from ``outcome`` for that reason: it is not an
    outcome this frame ever carries. Both are preceded by `run.metrics`.
    """

    type: Literal["run.ended"] = "run.ended"
    outcome: Literal["done", "blocked", "cancelled"]
    detail: str | None = None


class RunError(_Body):
    """The closing frame for a run that failed. See :class:`RunEnded` — these two are
    the same terminal position in the protocol, not a frame plus an extra."""

    type: Literal["run.error"] = "run.error"
    message: str
    kind: str | None = None


class StepStarted(_Body):
    type: Literal["step.started"] = "step.started"
    index: int
    title: str | None = None


class StepCompleted(_Body):
    type: Literal["step.completed"] = "step.completed"
    index: int


class ThinkingDelta(_Body):
    type: Literal["thinking.delta"] = "thinking.delta"
    text: str


class AnswerDelta(_Body):
    type: Literal["answer.delta"] = "answer.delta"
    text: str


class LimitNotice(_Body):
    type: Literal["limit.notice"] = "limit.notice"
    # "steps" | "tool_calls" | "tokens" | "time" | "loop" | "verify" | "context" | "search"
    # ("context" = the model's context window was exceeded; the run stops, it isn't degraded.
    # "search" was the deep-research pipeline's two-empty-rounds abort; nothing emits it now
    # that research is an ordinary thread, and it stays in the vocabulary because removing a
    # value a client already handles is a narrowing, which this protocol does not do.)
    limit: str
    message: str
    # The stop marker this notice announces, when the bound also blocked the turn — the
    # same string the turn persists as its `blocked_reason`. It is here so the toast and
    # the marker on the stopped turn can offer the *same* remedy: two context stops share
    # one `limit` value but not one answer, and prose is not something a client can key on.
    detail: str | None = None
