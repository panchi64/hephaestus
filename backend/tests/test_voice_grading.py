"""Grades: the schema, the aggregates, the check against a run, and the comparison."""

from __future__ import annotations

import json
from collections.abc import Sequence
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
    humor_tally,
    scenario_means,
    voice_score,
)
from evals.voice.record import RUN_FILE, Run, Sample, Turn
from evals.voice.spec import Scenario

type Line = dict[str, object]


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
    scores: dict[str, dict[str, int]],
    passed: bool = True,
    fingerprint: str = "f1",
    humor: Sequence[Line] | None = (),
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
            "humor": None if humor is None else list(humor),
        }
    )


def _run(**turns: int) -> Run:
    """A run with one sample per scenario id, each with that many turns."""
    return Run(
        group="g",
        name="A",
        started_at="now",
        prompt_fingerprint="f1",
        stub=False,
        samples=[
            Sample(scenario=sid, sample=1, turns=[Turn(prompt="p") for _ in range(n)])
            for sid, n in turns.items()
        ],
    )


def _line(sid: str, reply: int, *faults: str, device: str = "understatement") -> Line:
    return {
        "scenario": sid,
        "sample": 1,
        "reply": reply,
        "quote": "q",
        "device": device,
        "target": "task",
        "faults": list(faults),
    }


COMPLETE = {"shape": 3, "character": 3, "humor": 3, "helper": 3}


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
    grades = _grades({"a": {"shape": 1}}, humor=[_line("a", 1)])
    assert Grades.model_validate_json(grades.model_dump_json()) == grades


def test_check_names_every_mismatch() -> None:
    scenarios = [_scenario("a", ["shape"], 2), _scenario("b", ["shape"], 1)]
    found = problems(_grades({"a": {"shape": 3}}, fingerprint="f2"), _run(a=1, b=1), scenarios)
    assert "prompt_fingerprint does not match the run's" in found
    assert any("a #1: dimensions" in p for p in found)
    assert any("a #1: 1 expect results for 2 items" in p for p in found)
    assert "b #1: recorded but not graded" in found


def test_check_passes_a_complete_grading() -> None:
    scenarios = [_scenario("a", ["shape"], 1)]
    assert problems(_grades({"a": COMPLETE}), _run(a=1), scenarios) == []


def test_check_wants_the_humor_list_even_when_empty() -> None:
    scenarios = [_scenario("a", ["shape"], 1)]
    found = problems(_grades({"a": COMPLETE}, humor=None), _run(a=1), scenarios)
    assert any(p.startswith("humor: missing") for p in found)


def test_check_bounds_humor_lines_by_the_recorded_replies() -> None:
    scenarios = [_scenario("a", ["shape"], 1)]
    # A sub-agent delivery adds a Talos reply the scenario's prompts don't count.
    delivered = _grades({"a": COMPLETE}, humor=[_line("a", 2)])
    assert problems(delivered, _run(a=2), scenarios) == []
    stray = _grades({"a": COMPLETE}, humor=[_line("a", 3), _line("b", 1)])
    found = problems(stray, _run(a=2), scenarios)
    assert "humor a #1: reply 3 past the sample's last" in found
    assert "humor b #1: not a graded sample" in found


def test_check_rejects_a_faulted_line_under_a_full_humor_score() -> None:
    scenarios = [_scenario("a", ["shape"], 1)]
    grades = _grades({"a": COMPLETE}, humor=[_line("a", 1, "dark")])
    assert any("under a humor score of 3" in p for p in problems(grades, _run(a=1), scenarios))


def test_the_humor_tally_counts_rate_spread_and_faults() -> None:
    grades = _grades(
        {"long": {"shape": 3}, "short": {"shape": 3}, "quiet": {"shape": 3}, "flat": {"shape": 3}},
        humor=[
            _line("long", 2),
            _line("long", 3, "repeat", device="literal"),
            _line("short", 1, "dark"),
        ],
    )
    replies = {("long", 1): 6, ("short", 1): 1, ("quiet", 1): 2, ("flat", 1): 3}
    tally = humor_tally(grades, replies, quiet={"quiet"})
    assert tally is not None
    assert tally.lines == 3
    assert tally.reply_rate == 0.25
    # "quiet" is left out: one of the two other multi-reply samples carries a line.
    assert tally.conversation_rate == 0.5
    assert tally.devices == 2
    assert tally.back_to_back == 1
    assert tally.faults["repeat"] == 1 and tally.faults["dark"] == 1 and tally.faults["mean"] == 0


def test_grades_from_before_the_tally_have_none() -> None:
    assert humor_tally(_grades({"a": {"shape": 3}}, humor=None), {("a", 1): 1}, quiet=()) is None


def _graded(name: str, grades: Grades) -> Graded:
    return Graded(name, grades, {(s.id, 1): 1 for s in grades.scenarios})


def test_compare_two_runs_reports_deltas_movers_and_flips() -> None:
    before = _graded("g/A", _grades({"a": {"shape": 1, "helper": 1}, "b": {"shape": 3}}))
    after = _graded(
        "g/B",
        _grades({"a": {"shape": 3, "helper": 3}, "b": {"shape": 3}}, passed=False),
    )
    text = report([before, after], same_prompt_expected=True, quiet=())
    assert "| `shape` | 2.00 | 3.00 | +1.00 |" in text
    assert "| `helper` | 1.00 | 3.00 | +2.00 |" in text
    assert "- **a** 1.00 → 3.00" in text
    assert "  - g/A: shape 1: a shape" in text
    assert "- a #1: pass → fail" in text


def test_compare_shows_a_dash_for_a_run_graded_before_the_tally() -> None:
    old = _graded("g/A", _grades({"a": {"shape": 3}}, humor=None))
    new = _graded("g/B", _grades({"a": {"shape": 3}}))
    assert "| humor lines | – | 0 |" in report([old, new], same_prompt_expected=True, quiet=())


def test_compare_warns_when_prompts_differ_in_a_model_comparison() -> None:
    first = _graded("g/A", _grades({"a": {"shape": 3}}, fingerprint="f1"))
    second = _graded("g/B", _grades({"a": {"shape": 3}}, fingerprint="f2"))
    runs = [first, second]
    assert "different prompts" in report(runs, same_prompt_expected=True, quiet=())
    assert "same prompt" not in report(runs, same_prompt_expected=False, quiet=())


def test_a_group_reveals_its_models_from_the_key(tmp_path: Path) -> None:
    for letter in ("A", "B"):
        run_dir = tmp_path / "g" / letter
        run_dir.mkdir(parents=True)
        _ = (run_dir / "grades.json").write_text(_grades({"a": {"shape": 3}}).model_dump_json())
        _ = (run_dir / RUN_FILE).write_text(_run(a=2).model_dump_json())
    _ = (tmp_path / "g" / "key.json").write_text(json.dumps({"A": "muse", "B": "swift"}))
    runs = load_runs([str(tmp_path / "g")])
    assert [run.name for run in runs] == ["g/A (muse)", "g/B (swift)"]
    assert runs[0].replies == {("a", 1): 2}
