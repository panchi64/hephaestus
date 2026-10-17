"""The decision model's probabilities, combined into the reviewer's three axes.

Each axis reads its conservative value unless the probabilities clear it: an uncertain risk
is ``high`` and an uncertain authorization is ``neutral``, so a borderline call parks.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from core.config import Settings
from services.decisions import Answer, NoulAnswer, ScoreAnswer
from services.permissions.reviewer import Authorization, ReviewVerdict, Risk, ScoredVerdict

MISMATCH_NOTE = "the action may be aimed at something the operator did not name"


@dataclass(frozen=True)
class Thresholds:
    """The cut-offs, read from the ``decision_*`` settings, which hold the defaults."""

    #: ``P(irreversible)`` at or above this is ``too_destructive``.
    destroy_min: float
    #: ``low`` needs the weighted risk position at or below this…
    low_position_max: float
    #: …at most this much probability on the two upper levels…
    low_tail_max: float
    #: …and ``P(outward)`` below this.
    outward_max: float
    refused_min: float
    requested_min: float
    #: ``explicitly_yes`` also needs ``P(refused)`` below this.
    refused_clear_max: float
    mismatch_max: float

    @classmethod
    def from_settings(cls, settings: Settings) -> Thresholds:
        return cls(
            destroy_min=settings.decision_destroy_min,
            low_position_max=settings.decision_low_position_max,
            low_tail_max=settings.decision_low_tail_max,
            outward_max=settings.decision_outward_max,
            refused_min=settings.decision_refused_min,
            requested_min=settings.decision_requested_min,
            refused_clear_max=settings.decision_refused_clear_max,
            mismatch_max=settings.decision_mismatch_max,
        )


def score(answers: Mapping[str, Answer], thresholds: Thresholds) -> ScoredVerdict:
    """Combine the answers to ``decision_questions.QUESTIONS`` into one verdict.

    Raises ``KeyError`` or ``TypeError`` when an answer is missing or of the wrong type;
    the client has already checked both, so either is a bug.
    """
    risk_answer = answers["risk"]
    if not isinstance(risk_answer, ScoreAnswer):
        raise TypeError("risk must be a score answer")
    levels = risk_answer.probabilities
    outward = _noul(answers, "outward")
    requested = _noul(answers, "requested")
    refused = _noul(answers, "refused")
    mismatch = _noul(answers, "mismatch")

    risk: Risk
    if levels[-1] >= thresholds.destroy_min:
        risk = "too_destructive"
    elif (
        risk_answer.score <= thresholds.low_position_max
        and sum(levels[2:]) <= thresholds.low_tail_max
        and outward < thresholds.outward_max
    ):
        risk = "low"
    else:
        risk = "high"

    authorization: Authorization
    if refused >= thresholds.refused_min:
        authorization = "explicitly_no"
    elif (
        requested >= thresholds.requested_min
        and refused < thresholds.refused_clear_max
        and mismatch < thresholds.mismatch_max
    ):
        authorization = "explicitly_yes"
    else:
        authorization = "neutral"

    verdict = ReviewVerdict(
        risk=risk,
        authorization=authorization,
        correctness=MISMATCH_NOTE if mismatch >= thresholds.mismatch_max else None,
    )
    scores = {
        "risk": round(risk_answer.score, 3),
        "irreversible": round(levels[-1], 3),
        "outward": round(outward, 3),
        "requested": round(requested, 3),
        "refused": round(refused, 3),
        "mismatch": round(mismatch, 3),
    }
    return ScoredVerdict(verdict, scores)


def _noul(answers: Mapping[str, Answer], name: str) -> float:
    answer = answers[name]
    if not isinstance(answer, NoulAnswer):
        raise TypeError(f"{name} must be a noul answer")
    return answer.noul
