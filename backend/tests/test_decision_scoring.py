"""The probabilities, combined: each axis reads its conservative value unless cleared."""

from __future__ import annotations

import pytest

from core.config import Settings
from services.decisions import Answer, NoulAnswer, ScoreAnswer
from services.permissions.decision_scoring import MISMATCH_NOTE, Thresholds, score

T = Thresholds.from_settings(Settings())


def answers(
    *,
    levels: tuple[float, float, float, float] = (0.6, 0.4, 0.0, 0.0),
    outward: float = 0.0,
    requested: float = 0.95,
    refused: float = 0.0,
    mismatch: float = 0.0,
) -> dict[str, Answer]:
    position = sum(index * p for index, p in enumerate(levels))
    return {
        "risk": ScoreAnswer(score=position, probabilities=list(levels)),
        "outward": NoulAnswer(noul=outward),
        "requested": NoulAnswer(noul=requested),
        "refused": NoulAnswer(noul=refused),
        "mismatch": NoulAnswer(noul=mismatch),
    }


class TestRisk:
    def test_a_confident_workspace_change_is_low(self):
        assert score(answers(), T).verdict.risk == "low"

    def test_a_small_chance_of_the_irreversible_is_enough(self):
        verdict = score(answers(levels=(0.3, 0.3, 0.0, T.destroy_min)), T).verdict
        assert verdict.risk == "too_destructive"

    def test_just_under_the_destroy_bar_is_not_unrecoverable(self):
        verdict = score(answers(levels=(0.7, 0.0, 0.0, T.destroy_min - 0.01)), T).verdict
        assert verdict.risk != "too_destructive"

    def test_a_tail_on_the_upper_levels_is_high_even_when_the_mean_is_low(self):
        # Mean 0.6, but a fifth of the mass says it reaches beyond the workspace.
        verdict = score(answers(levels=(0.65, 0.1, 0.25, 0.0)), T).verdict
        assert verdict.risk == "high"

    def test_a_middling_position_is_high(self):
        assert score(answers(levels=(0.1, 0.9, 0.0, 0.0)), T).verdict.risk == "high"

    def test_anything_outward_is_high_whatever_the_levels_say(self):
        assert score(answers(outward=T.outward_max), T).verdict.risk == "high"


class TestAuthorization:
    def test_a_confident_request_is_an_explicit_yes(self):
        assert score(answers(), T).verdict.authorization == "explicitly_yes"

    def test_a_lukewarm_request_is_neutral(self):
        verdict = score(answers(requested=T.requested_min - 0.01), T).verdict
        assert verdict.authorization == "neutral"

    def test_a_refusal_outranks_a_request(self):
        verdict = score(answers(requested=0.99, refused=T.refused_min), T).verdict
        assert verdict.authorization == "explicitly_no"

    @pytest.mark.parametrize("refused", [0.2, 0.3, 0.49])
    def test_a_hint_of_refusal_withholds_the_yes(self, refused: float):
        assert score(answers(refused=refused), T).verdict.authorization == "neutral"

    def test_an_act_aimed_at_the_wrong_thing_is_not_a_yes(self):
        verdict = score(answers(mismatch=T.mismatch_max), T).verdict
        assert verdict.authorization == "neutral"
        assert verdict.correctness == MISMATCH_NOTE


def test_the_scores_ride_along_rounded():
    scored = score(answers(outward=0.12345), T)
    assert scored.scores["outward"] == 0.123
    assert set(scored.scores) == {
        "risk",
        "irreversible",
        "outward",
        "requested",
        "refused",
        "mismatch",
    }


def test_a_missing_answer_is_a_bug_not_a_verdict():
    incomplete = answers()
    del incomplete["refused"]
    with pytest.raises(KeyError):
        _ = score(incomplete, T)
