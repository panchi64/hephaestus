"""The Auto reviewer over the operator's ``decision``-role endpoint."""

from __future__ import annotations

import logging

import httpx

from services import decisions
from services.endpoint_spec import EndpointSpec
from services.permissions.decision_questions import QUESTIONS, build_state
from services.permissions.decision_scoring import Thresholds, score
from services.permissions.reviewer import (
    Reviewer,
    ReviewRequest,
    ReviewUnavailable,
    ScoredVerdict,
)

logger = logging.getLogger(__name__)


def make_decision_reviewer(
    spec: EndpointSpec,
    *,
    thresholds: Thresholds,
    timeout_s: float,
    client: httpx.AsyncClient | None = None,
) -> Reviewer:
    """A reviewer that asks every question in one call, bounded by ``timeout_s``."""

    async def review(request: ReviewRequest) -> ScoredVerdict | ReviewUnavailable:
        try:
            answers = await decisions.ask(
                spec, build_state(request), QUESTIONS, timeout_s=timeout_s, client=client
            )
        except decisions.DecisionUnavailable as exc:
            logger.warning("auto review failed for %s, parking: %s", request.capability.tool, exc)
            return ReviewUnavailable(str(exc))
        return score(answers, thresholds)

    return review
