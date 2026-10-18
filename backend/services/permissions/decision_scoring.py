"""The decision model's probabilities, combined into the reviewer's three axes.

Each axis reads its conservative value unless the probabilities clear it: an uncertain risk
is ``high`` and an uncertain authorization is ``neutral``, so a borderline call parks.
"""

from __future__ import annotations

from collections.abc import Mapping

from core.config import Settings
from services.decisions import Answer, NoulAnswer, ScoreAnswer
from services.permissions.reviewer import Authorization, ReviewVerdict, Risk, ScoredVerdict

MISMATCH_NOTE = "the action may be aimed at something the operator did not name"


def score(answers: Mapping[str, Answer], settings: Settings) -> ScoredVerdict:
    """Combine the answers to ``decision_questions.QUESTIONS`` into one verdict, against
    the ``decision_*`` thresholds in ``settings``.

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
    if levels[-1] >= settings.decision_destroy_min:
        risk = "too_destructive"
    elif (
        sum(levels[2:]) <= settings.decision_low_tail_max
        and levels[-1] <= settings.decision_low_irreversible_max
        and outward < settings.decision_outward_max
    ):
        risk = "low"
    else:
        risk = "high"

    authorization: Authorization
    if refused >= settings.decision_refused_min:
        authorization = "explicitly_no"
    elif (
        requested >= settings.decision_requested_min
        and refused < settings.decision_refused_clear_max
        and mismatch < settings.decision_mismatch_max
    ):
        authorization = "explicitly_yes"
    else:
        authorization = "neutral"

    verdict = ReviewVerdict(
        risk=risk,
        authorization=authorization,
        correctness=MISMATCH_NOTE if mismatch >= settings.decision_mismatch_max else None,
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
