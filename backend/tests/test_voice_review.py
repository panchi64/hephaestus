"""The review pass: corrections land on the first pass, never stack, and are measured."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from evals.voice.grading import GRADES_FILE, Grades
from evals.voice.record import RUN_FILE, Run, Sample, Turn
from evals.voice.review import (
    FIRST_FILE,
    REPORT_FILE,
    REVIEW_FILE,
    Review,
    agreement,
    apply_review,
    review_run,
)
from evals.voice.spec import Scenario

SCENARIO = Scenario.model_validate(
    {
        "id": "s",
        "voice": "talos",
        "about": "a",
        "prompts": ["p"],
        "dimensions": ["directness"],
        "expect": ["item (§5)", "other (§5)"],
    }
)


def _grades() -> Grades:
    return Grades.model_validate(
        {
            "prompt_fingerprint": "f1",
            "grader": "sonnet",
            "scenarios": [
                {
                    "id": "s",
                    "samples": [
                        {
                            "sample": 1,
                            "dimensions": {
                                d: {"score": 3}
                                for d in ("character", "directness", "humor", "helper")
                            },
                            "expect": [{"passed": True}, {"passed": True}],
                        }
                    ],
                }
            ],
            "humor": [],
        }
    )


def _review(**fields: object) -> Review:
    return Review.model_validate({"reviewer": "opus", **fields})


FIX = {
    "scenario": "s",
    "sample": 1,
    "dimension": "humor",
    "score": 1,
    "evidence": '"coffee"',
    "reason": "humor 1: stock line",
}


def test_corrections_land_and_name_the_reviewer() -> None:
    flip = {"scenario": "s", "sample": 1, "item": 2, "passed": False, "reason": "an offer"}
    final = apply_review(_grades(), _review(dimensions=[FIX], expect=[flip]))
    sample = final.scenarios[0].samples[0]
    assert sample.dimensions["humor"].score == 1
    assert [r.passed for r in sample.expect] == [True, False]
    assert final.grader == "sonnet; reviewed by opus"


@pytest.mark.parametrize(
    "fix",
    [
        {"dimensions": [{**FIX, "dimension": "judgment"}]},
        {"dimensions": [{**FIX, "sample": 2}]},
        {"expect": [{"scenario": "s", "sample": 1, "item": 3, "passed": False, "reason": "r"}]},
    ],
)
def test_a_correction_to_something_ungraded_is_refused(fix: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="no "):
        _ = apply_review(_grades(), _review(**fix))


def test_a_correction_below_three_still_needs_evidence() -> None:
    with pytest.raises(ValidationError):
        _ = apply_review(_grades(), _review(dimensions=[{**FIX, "evidence": ""}]))


def test_a_replaced_humor_list_needs_a_reason() -> None:
    with pytest.raises(ValidationError):
        _ = _review(humor=[])


def test_agreement_counts_changed_scores_and_flips() -> None:
    flip = {"scenario": "s", "sample": 1, "item": 1, "passed": False, "reason": "r"}
    first = _grades()
    measured = agreement(first, apply_review(first, _review(dimensions=[FIX], expect=[flip])))
    assert measured.rate == 0.75
    assert measured.changes == {"humor": [-2]}
    assert (measured.expect_flips, measured.expect_items) == (1, 2)


def test_reviewing_again_starts_from_the_first_pass(tmp_path: Path) -> None:
    run = Run(
        group="g",
        name="A",
        started_at="now",
        prompt_fingerprint="f1",
        stub=False,
        samples=[Sample(scenario="s", sample=1, turns=[Turn(prompt="p")])],
    )
    _ = (tmp_path / RUN_FILE).write_text(run.model_dump_json())
    _ = (tmp_path / GRADES_FILE).write_text(_grades().model_dump_json())
    _ = (tmp_path / REVIEW_FILE).write_text(_review(dimensions=[FIX]).model_dump_json())

    assert review_run(tmp_path, [SCENARIO]) == []
    assert (tmp_path / FIRST_FILE).is_file()
    assert "75% of 4" in (tmp_path / REPORT_FILE).read_text()
    assert (tmp_path / "grades.md").is_file()

    _ = (tmp_path / REVIEW_FILE).write_text(_review().model_dump_json())
    assert review_run(tmp_path, [SCENARIO]) == []
    final = Grades.model_validate_json((tmp_path / GRADES_FILE).read_text())
    assert final.scenarios[0].samples[0].dimensions["humor"].score == 3
    assert final.grader == "sonnet; reviewed by opus"


def test_a_missing_review_is_reported(tmp_path: Path) -> None:
    assert review_run(tmp_path, [SCENARIO])[0].startswith(REVIEW_FILE)
