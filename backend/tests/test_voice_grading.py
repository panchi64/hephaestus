"""Grades: the schema, the aggregates, the check against a run, and the comparison."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from evals.voice.check import problems
from evals.voice.compare import Graded, load_runs, report
from evals.voice.grading import (
    DimensionScore,
    Grades,
    dimension_means,
    expect_pass_rate,
    scenario_means,
    voice_score,
)
from evals.voice.record import Run, Sample
from evals.voice.spec import Scenario


def _scenario(sid: str, dimensions: list[str], expect: int) -> Scenario:
    return Scenario.model_validate(
        {
            "id": sid,
            "voice": "talos",
            "about": "a",
            "prompts": ["p"],
            "dimensions": dimensions,
            "expect": [f"item {i} (§5)" for i in range(expect)],
        }
    )


def _grades(
    scores: dict[str, dict[str, int]], passed: bool = True, fingerprint: str = "f1"
) -> Grades:
    return Grades.model_validate(
        {
            "prompt_fingerprint": fingerprint,
            "scenarios": [
                {
                    "id": sid,
                    "samples": [
                        {
                            "sample": 1,
                            "dimensions": {
                                d: {"score": s, "evidence": "" if s == 3 else f"{sid} {d}"}
                                for d, s in dims.items()
                            },
                            "expect": [{"passed": passed}],
                        }
                    ],
                }
                for sid, dims in scores.items()
            ],
        }
    )


def test_a_score_below_three_needs_evidence() -> None:
    with pytest.raises(ValidationError, match="evidence"):
        _ = DimensionScore(score=2)
    assert DimensionScore(score=3).evidence == ""


def test_scores_are_bounded() -> None:
    with pytest.raises(ValidationError):
        _ = DimensionScore(score=4, evidence="x")


def test_aggregates() -> None:
    grades = _grades({"a": {"shape": 3, "helper": 1}, "b": {"shape": 2, "helper": 3}})
    assert dimension_means(grades) == {"shape": 2.5, "helper": 2.0}
    assert scenario_means(grades) == {"a": 2.0, "b": 2.5}
    assert voice_score(grades) == 2.25
    assert expect_pass_rate(grades) == 1.0


def test_the_grades_round_trip() -> None:
    grades = _grades({"a": {"shape": 1}})
    assert Grades.model_validate_json(grades.model_dump_json()) == grades


def test_check_names_every_mismatch() -> None:
    scenarios = [_scenario("a", ["shape"], 2), _scenario("b", ["shape"], 1)]
    run = Run(
        group="g",
        name="A",
        started_at="now",
        prompt_fingerprint="f1",
        stub=False,
        samples=[Sample(scenario="a", sample=1), Sample(scenario="b", sample=1)],
    )
    found = problems(_grades({"a": {"shape": 3}}, fingerprint="f2"), run, scenarios)
    assert "prompt_fingerprint does not match the run's" in found
    assert any("a #1: dimensions" in p for p in found)
    assert any("a #1: 1 expect results for 2 items" in p for p in found)
    assert "b #1: recorded but not graded" in found


def test_check_passes_a_complete_grading() -> None:
    scenarios = [_scenario("a", ["shape"], 1)]
    run = Run(
        group="g",
        name="A",
        started_at="now",
        prompt_fingerprint="f1",
        stub=False,
        samples=[Sample(scenario="a", sample=1)],
    )
    assert problems(_grades({"a": {"shape": 3, "helper": 2}}), run, scenarios) == []


def test_compare_two_runs_reports_deltas_movers_and_flips() -> None:
    before = Graded("g/A", _grades({"a": {"shape": 1, "helper": 1}, "b": {"shape": 3}}))
    after = Graded(
        "g/B", _grades({"a": {"shape": 3, "helper": 3}, "b": {"shape": 3}}, passed=False)
    )
    text = report([before, after], same_prompt_expected=True)
    assert "| `shape` | 2.00 | 3.00 | +1.00 |" in text
    assert "| `helper` | 1.00 | 3.00 | +2.00 |" in text
    assert "- **a** 1.00 → 3.00" in text
    assert "  - g/A: shape 1: a shape" in text
    assert "- a #1: pass → fail" in text


def test_compare_warns_when_prompts_differ_in_a_model_comparison() -> None:
    first = Graded("g/A", _grades({"a": {"shape": 3}}, fingerprint="f1"))
    second = Graded("g/B", _grades({"a": {"shape": 3}}, fingerprint="f2"))
    assert "different prompts" in report([first, second], same_prompt_expected=True)
    assert "same prompt" not in report([first, second], same_prompt_expected=False)


def test_a_group_reveals_its_models_from_the_key(tmp_path: Path) -> None:
    for letter in ("A", "B"):
        run_dir = tmp_path / "g" / letter
        run_dir.mkdir(parents=True)
        _ = (run_dir / "grades.json").write_text(_grades({"a": {"shape": 3}}).model_dump_json())
    _ = (tmp_path / "g" / "key.json").write_text(json.dumps({"A": "muse", "B": "swift"}))
    assert [run.name for run in load_runs([str(tmp_path / "g")])] == ["g/A (muse)", "g/B (swift)"]
