"""The harness rubric's dimensions, the grades file a grading session writes, and the
issues it files."""

from __future__ import annotations

from pathlib import Path
from statistics import fmean
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

type Dimension = Literal[
    "outcome",
    "tool_use",
    "delegation",
    "coordination",
    "subagent",
    "solving",
    "operator_flow",
]

#: In rubric order. ``GRADING.md`` defines each one; a test keeps the two in step.
DIMENSIONS: tuple[Dimension, ...] = (
    "outcome",
    "tool_use",
    "delegation",
    "coordination",
    "subagent",
    "solving",
    "operator_flow",
)

#: Scored on every sample, so a scenario never lists them.
IMPLICIT: tuple[Dimension, ...] = ("outcome", "operator_flow")

#: ``platform`` is a product defect, ``behavior`` an agent going against its prompt or the
#: operator, ``ux`` something that works but confuses or slows the operator, ``eval`` a
#: fault in this suite's scenario, check or driver rather than the product.
type IssueKind = Literal["platform", "behavior", "ux", "eval"]
type Severity = Literal["blocker", "major", "minor", "polish"]
SEVERITIES: tuple[Severity, ...] = ("blocker", "major", "minor", "polish")

GRADES_FILE = "grades.json"


class _Strict(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)


class DimensionScore(_Strict):
    score: int = Field(ge=0, le=3)
    #: The quoted text the score rests on; required below 3.
    evidence: str = ""

    @model_validator(mode="after")
    def _evidence_below_three(self) -> DimensionScore:
        if self.score < 3 and not self.evidence.strip():
            raise ValueError("a score below 3 needs an evidence quote")
        return self


class ExpectResult(_Strict):
    passed: bool
    note: str = ""


class SampleGrade(_Strict):
    sample: int = Field(ge=1)
    dimensions: dict[Dimension, DimensionScore]
    #: One entry per item of the scenario's ``expect`` list, in order.
    expect: list[ExpectResult]


class ScenarioGrade(_Strict):
    id: str
    samples: list[SampleGrade] = Field(min_length=1)


class Issue(_Strict):
    title: str = Field(min_length=1)
    kind: IssueKind
    severity: Severity
    scenario: str
    sample: int = Field(ge=1)
    #: A quote from the transcript that shows it.
    evidence: str = Field(min_length=1)
    #: The file or component it likely lives in, when the transcript points at one.
    suspect: str = ""


class Grades(_Strict):
    #: The run's ``prompt_fingerprint``, copied from the transcript header.
    prompt_fingerprint: str
    grader: str = ""
    scenarios: list[ScenarioGrade]
    issues: list[Issue] = Field(default_factory=list)
    notes: str = ""


def load_grades(path: Path) -> Grades:
    """A grades file, or the one inside a run directory."""
    target = path / GRADES_FILE if path.is_dir() else path
    return Grades.model_validate_json(target.read_text())


def dimension_means(grades: Grades) -> dict[Dimension, float]:
    """Each graded dimension's mean over every sample that scored it."""
    scores: dict[Dimension, list[int]] = {}
    for scenario in grades.scenarios:
        for sample in scenario.samples:
            for dimension, scored in sample.dimensions.items():
                scores.setdefault(dimension, []).append(scored.score)
    return {d: round(fmean(scores[d]), 2) for d in DIMENSIONS if d in scores}


def expect_pass_rate(grades: Grades) -> float:
    results = [r.passed for s in grades.scenarios for sample in s.samples for r in sample.expect]
    return round(sum(results) / len(results), 3) if results else 0.0


def harness_score(grades: Grades) -> float:
    """The mean of the dimension means, so no dimension outweighs another for having more
    scenarios."""
    means = dimension_means(grades)
    return round(fmean(means.values()), 2) if means else 0.0
