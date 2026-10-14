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
    # Plain-language description of what the action does and its effect, so the
    # operator can judge it without reading the raw arguments — set when the tool
    # supplies one (the host-execution path requires it). Additive to v1; no bump.
    explanation: str | None = None


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
    """The turn is parked on the operator answering, not on them permitting.

    Its own event rather than an `approval.required` with a different shape, because the
    two ask for different things and are answered with different things. An approval is a
    yes or a no about an action already decided on; a question has no default, no safe
    side, and comes back carrying a *value* that becomes the tool's result. Folding one
    into the other would have made `approved` meaningless for half its uses.

    One event per call, carrying every question in it: the model asks for what it needs in
    one go, and the operator answers it in one go. Additive to v1; no bump."""

    type: Literal["question.asked"] = "question.asked"
    tool_call_id: str
    questions: list[QuestionSpec] = Field(default_factory=list)


class AnsweredQuestionOut(Body):
    """One question and what the operator said to it, as structure rather than as prose.

    The tool's *result* is a rendered paragraph, because that is what the model reads. A
    client asked to draw a card from it would have to parse that paragraph back apart —
    and a renderer that parses prose is a renderer that breaks the day the wording is
    improved. So the same answer rides here already taken apart, built from the parked
    call's own arguments and the operator's replies.

    ``selections`` is empty when they wrote instead of choosing, and ``text`` is null when
    they only chose; both are filled when they did both."""

    question: str
    selections: list[str] = Field(default_factory=list)
    text: str | None = None


class QuestionAnswered(Body):
    """The operator answered a parked ``ask_user`` call.

    The counterpart to :class:`QuestionAsked`, and it exists for the same reason that one
    does: the question arrives as structure, so the answer has to as well, or the card the
    client drew from the question has nothing but the tool result's prose to complete
    itself with. Emitted per answered call, never per question.

    What it carries is derived from the **parked call's arguments** plus the replies the
    server validated against them — never from labels the client sent — so the answer in
    the transcript cannot say something the question never offered. Additive to v1; no
    bump."""

    type: Literal["question.answered"] = "question.answered"
    tool_call_id: str
    answers: list[AnsweredQuestionOut] = Field(default_factory=list)


class ReviewStarted(Body):
    """An action at the Auto level is being ruled on in the operator's place.

    Auto's proposition is that the operator's approvals are given for them. The only thing
    that makes that acceptable is that they can see it happening and read afterwards what
    was decided and why — so the review announces itself before it runs, rather than a
    tool call simply appearing to have been made.

    ``summary`` is the action's extracted worst case, in the same words the reviewer is
    judging and the operator can read. ``detail`` is the act's own content where the tool
    has some worth reading — a delegated task, a program, the reason given for opening a
    credential — and null where there is none, which is most tools. ``reach`` is how far a
    shell command *declared* it needs to go — the worktree, the network, or the host — and
    is null for every kind of act that declares nothing, which is a different fact from
    declaring the widest reach. Additive to v1; no bump."""

    type: Literal["review.started"] = "review.started"
    tool_call_id: str
    name: str
    summary: str
    detail: str | None = None
    reach: Literal["workspace", "network", "host"] | None = None


class ReviewCompleted(Body):
    """How the review ruled, on the three axes it ruled on.

    ``decision`` is the outcome the run then took — ``allow`` ran the call without a
    prompt, ``ask`` parked it for the operator anyway. ``block``, which refused the call
    outright, is no longer produced: an act the reviewer judged unrecoverable parks like
    anything else it will not clear, since that is the one act the operator most needs put
    in front of them. The word stays in the vocabulary because it is already written into
    the stored events of threads reviewed before that changed. It
    carries deliberately more than the outcome: ``stage`` says whether the structural judge
    or a model settled it, ``tier`` says on which ground when the judge did, ``fenced``
    whether an OS fence was there to hold the command to what it declared, and the three
    axes say what the model saw. An operator reading only "allowed" learns nothing they can
    act on; one reading "cleared structurally, fenced to the worktree" can tell an
    over-permissive rule from a well-judged call.

    The axes are null when the model stage never ran — the judge cleared it, or nothing
    was available to review with — and ``tier`` is null in the mirror case, whenever the
    model settled it. Additive to v1; no bump."""

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
