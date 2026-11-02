"""Apply a reviewer's corrections to a harness run's first-pass grades and measure how much
the first pass got right.

    uv run python -m evals.harness.review <group>/<letter> [...]

The reviewer writes ``review.json`` (``REVIEW.md`` is its standard). The first run keeps
the grader's ``grades.json`` as ``grades.first.json``; every run applies the review to
that copy, so rerunning after the reviewer edits ``review.json`` never stacks corrections.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

from pydantic import Field, ValidationError

from evals.drive.review import (
    FIRST_FILE,
    REPORT_FILE,
    REVIEW_FILE,
    ExpectFix,
    first_pass,
    review_main,
)
from evals.drive.spec import Strict as _Strict

from .backlog import read_backlog
from .check import problems
from .grading import (
    GRADES_FILE,
    Dimension,
    DimensionScore,
    ExpectResult,
    Grades,
    Issue,
    SampleGrade,
    load_grades,
)
from .record import load_run
from .spec import Scenario, load_scenarios, run_path

__all__ = ["FIRST_FILE", "REPORT_FILE", "REVIEW_FILE", "ExpectFix", "Review", "apply_review"]


class DimensionFix(_Strict):
    scenario: str
    sample: int = Field(ge=1)
    dimension: Dimension
    score: int = Field(ge=0, le=3)
    evidence: str = ""
    reason: str = Field(min_length=1)


class IssueRef(_Strict):
    scenario: str
    sample: int = Field(ge=1)
    title: str
    reason: str = Field(min_length=1)


class IssueChange(_Strict):
    #: The title of the first-pass issue this replaces, in the same sample.
    was: str
    issue: Issue
    reason: str = Field(min_length=1)


class Review(_Strict):
    reviewer: str = Field(min_length=1)
    dimensions: list[DimensionFix] = Field(default_factory=list)
    expect: list[ExpectFix] = Field(default_factory=list)
    issues_removed: list[IssueRef] = Field(default_factory=list)
    issues_changed: list[IssueChange] = Field(default_factory=list)
    issues_added: list[Issue] = Field(default_factory=list)
    notes: str | None = None
    rule_gaps: list[str] = Field(default_factory=list)


def _key(issue: Issue) -> tuple[str, int, str]:
    return issue.scenario, issue.sample, issue.title


def apply_review(first: Grades, review: Review) -> Grades:
    """The first pass with the review's corrections in place; raises ValueError for a
    correction naming a score, expect item or issue the first pass didn't have."""
    graded = {(s.id, sample.sample): sample for s in first.scenarios for sample in s.samples}
    for fix in review.dimensions:
        sample = graded.get((fix.scenario, fix.sample))
        if sample is None or fix.dimension not in sample.dimensions:
            raise ValueError(f"{fix.scenario} #{fix.sample}: no {fix.dimension} score to fix")
    for fix in review.expect:
        sample = graded.get((fix.scenario, fix.sample))
        if sample is None or fix.item > len(sample.expect):
            raise ValueError(f"{fix.scenario} #{fix.sample}: no expect item {fix.item}")
    issues = {_key(issue): issue for issue in first.issues}
    for ref in review.issues_removed:
        if issues.pop((ref.scenario, ref.sample, ref.title), None) is None:
            raise ValueError(f"{ref.scenario} #{ref.sample}: no issue titled {ref.title!r}")
    for change in review.issues_changed:
        old = (change.issue.scenario, change.issue.sample, change.was)
        if issues.pop(old, None) is None:
            raise ValueError(f"{old[0]} #{old[1]}: no issue titled {change.was!r}")
        issues[_key(change.issue)] = change.issue
    for issue in review.issues_added:
        issues[_key(issue)] = issue

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
            "issues": list(issues.values()),
            "notes": first.notes if review.notes is None else review.notes,
            "grader": f"{first.grader}; reviewed by {review.reviewer}",
        }
    )


@dataclass(frozen=True, slots=True)
class Agreement:
    scores: int
    changed: int
    expect_items: int
    expect_flips: int
    issues_first: int
    issues_corrected: int

    @property
    def rate(self) -> float:
        """Share of dimension scores the review left as they were."""
        return round(1 - self.changed / self.scores, 3) if self.scores else 1.0


def agreement(first: Grades, final: Grades, review: Review) -> Agreement:
    def scores(grades: Grades) -> dict[tuple[str, int, str], int]:
        return {
            (scenario.id, sample.sample, d): s.score
            for scenario in grades.scenarios
            for sample in scenario.samples
            for d, s in sample.dimensions.items()
        }

    def expects(grades: Grades) -> dict[tuple[str, int, int], bool]:
        return {
            (scenario.id, sample.sample, i): r.passed
            for scenario in grades.scenarios
            for sample in scenario.samples
            for i, r in enumerate(sample.expect)
        }

    before, after = scores(first), scores(final)
    old, new = expects(first), expects(final)
    return Agreement(
        scores=len(before),
        changed=sum(before[k] != after[k] for k in before),
        expect_items=len(old),
        expect_flips=sum(old[k] != new[k] for k in old),
        issues_first=len(first.issues),
        issues_corrected=len(review.issues_removed)
        + len(review.issues_changed)
        + len(review.issues_added),
    )


def render_review(review: Review, measured: Agreement) -> str:
    out = [
        "# Review",
        "",
        f"- reviewer: {review.reviewer}",
        f"- score agreement: {measured.rate:.0%} of {measured.scores} dimension scores unchanged",
        f"- expect items flipped: {measured.expect_flips} of {measured.expect_items}",
        f"- issues: {measured.issues_corrected} corrections to {measured.issues_first} filed",
        "",
        "## Corrections",
        "",
    ]
    fixes = [
        f"- `{f.scenario}` #{f.sample} `{f.dimension}` → {f.score}: {f.reason}"
        for f in review.dimensions
    ]
    fixes += [
        f"- `{f.scenario}` #{f.sample} expect {f.item} → {'pass' if f.passed else 'fail'}: "
        + f.reason
        for f in review.expect
    ]
    fixes += [
        f"- removed {r.title!r} (`{r.scenario}` #{r.sample}): {r.reason}"
        for r in review.issues_removed
    ]
    fixes += [f"- changed {c.was!r} → {c.issue.title!r}: {c.reason}" for c in review.issues_changed]
    fixes += [f"- added {i.title!r} (`{i.scenario}` #{i.sample})" for i in review.issues_added]
    out += [*(fixes or ["None."]), ""]
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
    backlog_ids = frozenset(e.id for e in read_backlog().entries)
    found = problems(final, load_run(run_dir), scenarios, backlog_ids)
    if found:
        return found
    _ = (run_dir / GRADES_FILE).write_text(final.model_dump_json(indent=2) + "\n")
    _ = (run_dir / REPORT_FILE).write_text(render_review(review, agreement(first, final, review)))
    return []


def main(argv: list[str]) -> int:
    scenarios = load_scenarios() if argv else []
    return review_main(argv, __doc__, lambda run_dir: review_run(run_dir, scenarios), run_path)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
