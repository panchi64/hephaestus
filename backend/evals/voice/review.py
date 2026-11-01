"""Apply a reviewer's corrections to a run's first-pass grades and measure how much the
first pass got right.

    uv run python -m evals.voice.review <group>/<letter> [...]

The reviewer writes ``review.json`` (``REVIEW.md`` is its standard). The first run keeps
the grader's ``grades.json`` as ``grades.first.json``; every run applies the review to
that copy, so rerunning after the reviewer edits ``review.json`` never stacks corrections.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from evals.drive.review import (
    FIRST_FILE,
    REPORT_FILE,
    REVIEW_FILE,
    ExpectFix,
    first_pass,
    review_main,
)

from .check import problems
from .grading import (
    DIMENSIONS,
    GRADES_FILE,
    Dimension,
    DimensionScore,
    ExpectResult,
    Grades,
    HumorLine,
    SampleGrade,
    WorstReply,
    load_grades,
)
from .record import load_run
from .spec import Scenario, load_scenarios, run_path
from .summary import write_summary

__all__ = ["FIRST_FILE", "REPORT_FILE", "REVIEW_FILE", "ExpectFix", "Review", "apply_review"]

_VERDICT = {True: "pass", False: "fail"}


class _Strict(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)


class DimensionFix(_Strict):
    scenario: str
    sample: int = Field(ge=1)
    dimension: Dimension
    score: int = Field(ge=0, le=3)
    evidence: str = ""
    #: The rule the first pass broke: a `GRADING.md` anchor or cap, or a voice.md section.
    reason: str = Field(min_length=1)


class Review(_Strict):
    reviewer: str = Field(min_length=1)
    dimensions: list[DimensionFix] = Field(default_factory=list)
    expect: list[ExpectFix] = Field(default_factory=list)
    #: The whole humor list, replaced only when the first pass's was wrong.
    humor: list[HumorLine] | None = None
    humor_reason: str = ""
    worst: list[WorstReply] | None = Field(default=None, max_length=5)
    notes: str | None = None
    #: Places `GRADING.md` was unclear enough to cause a misgrade.
    rule_gaps: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _humor_needs_reason(self) -> Review:
        if self.humor is not None and not self.humor_reason.strip():
            raise ValueError("a replaced humor list needs humor_reason")
        return self


def apply_review(first: Grades, review: Review) -> Grades:
    """The first pass with the review's corrections in place; raises ValueError for a
    correction that names a sample, dimension or expect item the first pass didn't grade."""
    graded = {(s.id, sample.sample): sample for s in first.scenarios for sample in s.samples}
    for fix in review.dimensions:
        sample = graded.get((fix.scenario, fix.sample))
        if sample is None or fix.dimension not in sample.dimensions:
            raise ValueError(f"{fix.scenario} #{fix.sample}: no {fix.dimension} score to fix")
    for fix in review.expect:
        sample = graded.get((fix.scenario, fix.sample))
        if sample is None or fix.item > len(sample.expect):
            raise ValueError(f"{fix.scenario} #{fix.sample}: no expect item {fix.item}")

    def corrected(scenario: str, sample: SampleGrade) -> SampleGrade:
        dimensions = dict(sample.dimensions)
        expect = list(sample.expect)
        for fix in review.dimensions:
            if (fix.scenario, fix.sample) == (scenario, sample.sample):
                dimensions[fix.dimension] = DimensionScore(score=fix.score, evidence=fix.evidence)
        for fix in review.expect:
            if (fix.scenario, fix.sample) == (scenario, sample.sample):
                expect[fix.item - 1] = ExpectResult(passed=fix.passed, note=fix.note)
        return sample.model_copy(update={"dimensions": dimensions, "expect": expect})

    scenarios = [
        s.model_copy(update={"samples": [corrected(s.id, sample) for sample in s.samples]})
        for s in first.scenarios
    ]
    return first.model_copy(
        update={
            "scenarios": scenarios,
            "humor": first.humor if review.humor is None else review.humor,
            "worst": first.worst if review.worst is None else review.worst,
            "notes": first.notes if review.notes is None else review.notes,
            "grader": f"{first.grader}; reviewed by {review.reviewer}",
        }
    )


@dataclass(frozen=True, slots=True)
class Agreement:
    scores: int
    #: Each changed score's ``final - first``, by dimension.
    changes: dict[Dimension, list[int]] = field(default_factory=dict)
    expect_items: int = 0
    expect_flips: int = 0

    @property
    def rate(self) -> float:
        """Share of dimension scores the review left as they were."""
        changed = sum(len(deltas) for deltas in self.changes.values())
        return round(1 - changed / self.scores, 3) if self.scores else 1.0


def agreement(first: Grades, final: Grades) -> Agreement:
    def by_sample(grades: Grades) -> dict[tuple[str, int], dict[Dimension, int]]:
        return {
            (scenario.id, sample.sample): {d: s.score for d, s in sample.dimensions.items()}
            for scenario in grades.scenarios
            for sample in scenario.samples
        }

    def expects(grades: Grades) -> dict[tuple[str, int, int], bool]:
        return {
            (scenario.id, sample.sample, i): result.passed
            for scenario in grades.scenarios
            for sample in scenario.samples
            for i, result in enumerate(sample.expect)
        }

    before, after = by_sample(first), by_sample(final)
    changes: dict[Dimension, list[int]] = {}
    scores = 0
    for key, dims in before.items():
        for dimension, score in dims.items():
            scores += 1
            delta = after[key][dimension] - score
            if delta:
                changes.setdefault(dimension, []).append(delta)
    old, new = expects(first), expects(final)
    return Agreement(
        scores=scores,
        changes={d: changes[d] for d in DIMENSIONS if d in changes},
        expect_items=len(old),
        expect_flips=sum(old[k] != new[k] for k in old),
    )


def render_review(review: Review, measured: Agreement) -> str:
    out = [
        "# Review",
        "",
        f"- reviewer: {review.reviewer}",
        f"- score agreement: {measured.rate:.0%} of {measured.scores} dimension scores unchanged",
        f"- expect items flipped: {measured.expect_flips} of {measured.expect_items}",
        f"- humor list replaced: {'no' if review.humor is None else review.humor_reason}",
        "",
    ]
    if measured.changes:
        out += ["| Dimension | Changed | Mean change |", "|---|---|---|"]
        out += [
            f"| `{d}` | {len(deltas)} | {sum(deltas) / len(deltas):+.2f} |"
            for d, deltas in measured.changes.items()
        ]
        out.append("")
    fixes = [
        f"- `{f.scenario}` #{f.sample} `{f.dimension}` → {f.score}: {f.reason}"
        for f in review.dimensions
    ] + [
        f"- `{f.scenario}` #{f.sample} expect {f.item} → {_VERDICT[f.passed]}: {f.reason}"
        for f in review.expect
    ]
    out += ["## Corrections", "", *(fixes or ["None."]), ""]
    out += ["## Rule gaps", "", *(f"- {gap}" for gap in review.rule_gaps or ["None."]), ""]
    return "\n".join(out)


def review_run(run_dir: Path, scenarios: list[Scenario]) -> list[str]:
    """Apply ``review.json`` and write the results; returns the problems that stopped it."""
    try:
        review = Review.model_validate_json((run_dir / REVIEW_FILE).read_text())
    except (OSError, ValidationError) as exc:
        return [f"{REVIEW_FILE}: {exc}"]
    first = load_grades(first_pass(run_dir, GRADES_FILE))
    try:
        final = apply_review(first, review)
    except (ValueError, ValidationError) as exc:
        return [str(exc)]
    found = problems(final, load_run(run_dir), scenarios)
    if found:
        return found
    _ = (run_dir / GRADES_FILE).write_text(final.model_dump_json(indent=2) + "\n")
    write_summary(run_dir, {s.id for s in scenarios if s.quiet})
    _ = (run_dir / REPORT_FILE).write_text(render_review(review, agreement(first, final)))
    return []


def main(argv: list[str]) -> int:
    scenarios = load_scenarios() if argv else []
    return review_main(argv, __doc__, lambda run_dir: review_run(run_dir, scenarios), run_path)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
