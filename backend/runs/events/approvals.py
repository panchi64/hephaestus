"""Operator-in-the-loop events: approvals and questions."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from .base import Body


class ApprovalRequired(Body):
    """A sensitive action is parked awaiting operator approval."""

    type: Literal["approval.required"] = "approval.required"
    tool_call_id: str
    name: str
    args: dict[str, object] = Field(default_factory=dict)
    summary: str
    # Plain-language effect so the operator needn't read raw arguments; set when the tool
    # supplies one (host execution requires it). Additive to v1.
    explanation: str | None = None
    # Why this call always asks: set for a guarded, once-only call, so a client offers no
    # standing approval. Additive to v1.
    guard: str | None = None
    # Why a host command is about to run with no fence in force (disabled by the operator
    # or unbuildable). Additive to v1.
    unconfined: str | None = None
    # Why the Auto level parked this call rather than running it ("runs on the host", "the
    # VM can reach the network"). Additive to v1.
    auto_reason: str | None = None


class ApprovalDecided(Body):
    """The operator settled a parked approval.

    A replay holds the ``approval.required`` that opened the card but nothing of the click,
    and the call's own result arrives only once it finishes, so without this a reattach
    while an approved call runs would reopen a prompt the run has already moved past.
    Additive to v1."""

    type: Literal["approval.decided"] = "approval.decided"
    tool_call_id: str
    approved: bool


class QuestionOption(Body):
    """One answer offered for a question."""

    label: str
    description: str | None = None


class QuestionSpec(Body):
    """One question the operator is being asked."""

    question: str
    options: list[QuestionOption] = Field(default_factory=list)
    multi_select: bool = False


class QuestionAsked(Body):
    """The turn is parked on the operator answering, not permitting.

    Its own event, not an `approval.required` variant: an approval is a yes or no about a
    decided action, while a question has no default or safe side and returns a value that
    becomes the tool's result. Merging them would make `approved` meaningless for half its uses.

    One event per call carrying every question, as the model asks and the operator answers
    in one go. Additive to v1."""

    type: Literal["question.asked"] = "question.asked"
    tool_call_id: str
    questions: list[QuestionSpec] = Field(default_factory=list)


class AnsweredQuestionOut(Body):
    """One question and the operator's answer, as structure rather than prose.

    The tool's result is a rendered paragraph for the model; a client parsing it would break
    when the wording improves, so the answer rides here taken apart, built from the parked
    call's arguments and the replies.

    ``selections`` is empty when they only wrote, ``text`` null when they only chose."""

    question: str
    selections: list[str] = Field(default_factory=list)
    text: str | None = None


class QuestionAnswered(Body):
    """The operator answered a parked ``ask_user`` call.

    The counterpart to :class:`QuestionAsked`: the question arrives as structure, so the
    answer must too, or the client's card has only the result's prose. Emitted per answered
    call, never per question.

    Derived from the parked call's arguments plus the validated replies, never client-sent
    labels, so the transcript can't say something the question never offered. Additive to v1."""

    type: Literal["question.answered"] = "question.answered"
    tool_call_id: str
    answers: list[AnsweredQuestionOut] = Field(default_factory=list)
