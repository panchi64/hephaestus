"""The Auto reviewer over the operator's ``decision``-role endpoint."""

from __future__ import annotations

import logging
from dataclasses import replace

import httpx

from core.config import Settings
from services import decisions
from services.endpoint_spec import EndpointSpec
from services.permissions.capability import capability_of
from services.permissions.decision_questions import QUESTIONS, build_state
from services.permissions.decision_scoring import score
from services.permissions.reviewer import (
    MESSAGE_CHARS,
    PriorApproval,
    Reviewer,
    ReviewRequest,
    ReviewUnavailable,
    ScoredVerdict,
    TranscriptEntry,
    fit_transcript,
)

logger = logging.getLogger(__name__)

#: The bind-time probe's fence token, so a scripted server can tell it apart. Fixed is safe
#: here alone: every fenced byte of the probe is this module's own filler.
PROBE_NONCE = "bind-probe"

#: Tokenizes about as densely as pasted code, the densest text a transcript carries.
_FILLER = "src/app/module_17.py:42: assert value == expected  # 0x1f\n"


def make_decision_reviewer(
    spec: EndpointSpec, *, settings: Settings, client: httpx.AsyncClient | None = None
) -> Reviewer:
    """A reviewer that asks every question in one call, bounded by ``review_timeout_s``
    and scored against the ``decision_*`` thresholds."""

    async def review(request: ReviewRequest) -> ScoredVerdict | ReviewUnavailable:
        try:
            answers = await decisions.ask(
                spec,
                build_state(request),
                QUESTIONS,
                timeout_s=settings.review_timeout_s,
                client=client,
            )
        except decisions.DecisionUnavailable as exc:
            logger.warning("auto review failed for %s, parking: %s", request.capability.tool, exc)
            return ReviewUnavailable(str(exc))
        return score(answers, settings)

    return review


async def probe(
    spec: EndpointSpec, *, settings: Settings, client: httpx.AsyncClient | None = None
) -> None:
    """Ask the review's own questions about a review at the transcript and ledger limits.

    A server that answers short prompts but refuses or times out on a full review (an engine
    that reads the whole prompt in one batch, with too small a batch) is rejected when the
    role is bound, rather than parking every review after it.

    Raises:
        decisions.DecisionUnavailable: The endpoint did not answer every question.
    """
    text = (_FILLER * (MESSAGE_CHARS // len(_FILLER) + 1))[:MESSAGE_CHARS]
    capability = replace(capability_of("shell_run_command", {"command": text}), detail=text)
    # One more entry than the window: the turn's opening request rides on top of it.
    entries = [
        TranscriptEntry("operator" if i % 2 == 0 else "assistant", f"{i} {text}"[:MESSAGE_CHARS])
        for i in range(settings.review_transcript_entries + 1)
    ]
    transcript = fit_transcript(
        entries, pinned=entries[:1], total_chars=settings.review_transcript_chars
    )
    approved = [
        PriorApproval(capability.tool, capability.summary, "operator")
        for _ in range(settings.review_ledger_entries)
    ]
    request = ReviewRequest(capability, transcript, approved, nonce=PROBE_NONCE)
    _ = await decisions.ask(
        spec,
        build_state(request),
        QUESTIONS,
        timeout_s=settings.review_probe_timeout_s,
        client=client,
    )
