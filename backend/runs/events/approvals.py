"""Operator-in-the-loop events: approvals, questions, and Auto-level reviews."""

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


class ReviewStarted(Body):
    """An action at the Auto level is being ruled on in the operator's place.

    Auto gives the operator's approvals for them, acceptable only if they can see it happen
    and read what was decided and why, so the review announces itself before running.

    ``summary`` is the action's extracted worst case, in the words the reviewer judges.
    ``detail`` is the act's own content where worth reading (a delegated task, a program,
    the reason for opening a credential), else null. ``reach`` is ``host`` for the host
    command tool, ``offline`` for a VM shell command, null for any other act (``network``
    is not produced). ``network`` is whether a VM command names an address or fetches,
    null where nothing runs in the VM.
    Additive to v1."""

    type: Literal["review.started"] = "review.started"
    tool_call_id: str
    name: str
    summary: str
    detail: str | None = None
    reach: Literal["offline", "network", "host"] | None = None
    network: bool | None = None


class ReviewCompleted(Body):
    """How the review ruled, on the three axes it ruled on.

    ``decision`` is the outcome the run took: ``allow`` ran the call without a prompt,
    ``ask`` parked it. ``block`` is no longer produced (an act judged unrecoverable parks,
    being the one the operator most needs to see) but stays in the vocabulary because stored
    events of earlier threads carry it. It carries more than the outcome: ``stage`` says
    whether the structural judge or a model settled it, ``tier`` on which ground when the
    judge did, ``fenced`` whether an OS fence held the command to what it declared, and the
    axes what the model saw. "Allowed" alone isn't actionable; "cleared structurally, fenced
    to the worktree" tells an over-permissive rule from a well-judged call.

    The axes are null when the model stage never ran (the judge cleared it, or no reviewer
    was available), and ``tier`` is null whenever the model settled it. Additive to v1."""

    type: Literal["review.completed"] = "review.completed"
    tool_call_id: str
    name: str
    decision: Literal["allow", "ask", "block"]
    stage: Literal["judge", "reviewer"]
    reason: str
    tier: Literal["read", "sandbox", "workspace"] | None = None
    #: Whether this host could confine the command at all. False is why an ordinary
    #: contained command reached a model reviewer, and it is the operator's to fix.
    fenced: bool = False
    risk: Literal["low", "high", "too_destructive"] | None = None
    authorization: Literal["explicitly_no", "neutral", "explicitly_yes"] | None = None
    correctness: str | None = None
    #: The decision model's probabilities behind the three axes, by question.
    scores: dict[str, float] | None = None
    #: Set when the model stage was needed and could not run: no decision model is
    #: bound, or the bound one did not answer. The client offers to set one up.
    setup: Literal["unbound", "unreachable"] | None = None
