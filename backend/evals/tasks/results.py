"""One line of a results file: how a task's turns ended and how it scored."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

type Outcome = Literal["pass", "partial", "fail", "skipped"]


class TurnRecord(BaseModel):
    #: The run's terminal outcome, ``error``, or ``timeout`` when no terminal event came.
    ended: str
    detail: str | None = None
    steps: int = 0
    tool_calls: int = 0
    tool_failures: list[str] = Field(default_factory=list)
    limits: list[str] = Field(default_factory=list)
    approvals: int = 0
    questions: int = 0
    #: Seconds from each model request's start to its first streamed token.
    first_token_s: list[float] = Field(default_factory=list)
    wall_s: float = 0.0


class TaskResult(BaseModel):
    id: str
    area: str
    outcome: Outcome
    reason: str = ""
    checks: dict[str, bool] = Field(default_factory=dict)
    judge_score: int | None = None
    turns: list[TurnRecord] = Field(default_factory=list)
    conversation_id: str | None = None
