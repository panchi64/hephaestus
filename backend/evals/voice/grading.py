"""The rubric's dimensions, the grades file a grading session writes, and its aggregates."""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from pathlib import Path
from statistics import fmean
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

type Dimension = Literal[
    "character",
    "directness",
    "shape",
    "boilerplate",
    "register",
    "humor",
    "judgment",
    "working",
    "helper",
    "report",
]

#: In rubric order. ``GRADING.md`` defines each one; a test keeps the two in step.
DIMENSIONS: tuple[Dimension, ...] = (
    "character",
    "directness",
    "shape",
    "boilerplate",
    "register",
    "humor",
    "judgment",
    "working",
    "helper",
    "report",
)

#: Scored on every sample, so a scenario never lists them.
IMPLICIT: tuple[Dimension, ...] = ("character", "humor", "helper")

type Device = Literal["understatement", "literal", "deadpan", "callback", "other"]
type Target = Literal["task", "result", "machine", "situation", "operator"]
type Fault = Literal["stock", "forced", "misplaced", "repeat", "dark", "mean", "untrue"]
FAULTS: tuple[Fault, ...] = ("stock", "forced", "misplaced", "repeat", "dark", "mean", "untrue")

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


class WorstReply(_Strict):
    scenario: str
    sample: int
    quote: str
    reason: str


class HumorLine(_Strict):
    scenario: str
    sample: int = Field(ge=1)
    #: Which of the sample's turns carries it, from 1, sub-agent deliveries included.
    reply: int = Field(ge=1)
    quote: str
    device: Device
    target: Target
    faults: list[Fault] = Field(default_factory=list)


class Grades(_Strict):
    #: The run's ``prompt_fingerprint``, copied from the transcript header.
    prompt_fingerprint: str
    grader: str = ""
    scenarios: list[ScenarioGrade]
    #: Every humorous line in the run, so frequency and repetition are counted run-wide.
    #: None only in grades written before the tally existed.
    humor: list[HumorLine] | None = None
    worst: list[WorstReply] = Field(default_factory=list, max_length=5)
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


def scenario_means(grades: Grades) -> dict[str, float]:
    """Each scenario's mean over all its samples and dimensions."""
    return {
        scenario.id: round(
            fmean(s.score for sample in scenario.samples for s in sample.dimensions.values()),
            2,
        )
        for scenario in grades.scenarios
        if any(sample.dimensions for sample in scenario.samples)
    }


def expect_pass_rate(grades: Grades) -> float:
    results = [r.passed for s in grades.scenarios for sample in s.samples for r in sample.expect]
    return round(sum(results) / len(results), 3) if results else 0.0


@dataclass(frozen=True, slots=True)
class HumorTally:
    lines: int
    #: Share of replies carrying a line; §6 wants most to carry none.
    reply_rate: float
    #: Share of multi-reply samples with a line, leaving out scenarios where §6 wants none.
    conversation_rate: float
    devices: int
    #: Lines in a reply right after one that also carried a line.
    back_to_back: int
    faults: dict[Fault, int]


def humor_tally(
    grades: Grades, replies: Mapping[tuple[str, int], int], quiet: Collection[str]
) -> HumorTally | None:
    """The run-wide humor counts, or None for grades written before the tally existed.

    ``replies`` is each recorded sample's Talos reply count, deliveries included, keyed by
    ``(scenario, sample)``; ``quiet`` holds the scenarios where §6 wants no line.
    """
    if grades.humor is None:
        return None
    samples = [(s.id, sample.sample) for s in grades.scenarios for sample in s.samples]
    total = sum(replies.get(key, 0) for key in samples)
    multi = [key for key in samples if replies.get(key, 0) > 1 and key[0] not in quiet]
    carrying = {(line.scenario, line.sample, line.reply) for line in grades.humor}
    with_line = {(sid, n) for sid, n, _ in carrying}
    return HumorTally(
        lines=len(grades.humor),
        reply_rate=round(len(carrying) / total, 2) if total else 0.0,
        conversation_rate=round(sum(k in with_line for k in multi) / len(multi), 2)
        if multi
        else 0.0,
        devices=len({line.device for line in grades.humor}),
        back_to_back=sum((sid, n, r - 1) in carrying for sid, n, r in carrying),
        faults={f: sum(f in line.faults for line in grades.humor) for f in FAULTS},
    )


def voice_score(grades: Grades) -> float:
    """The headline: the mean of the dimension means, so no dimension outweighs another
    for having more scenarios."""
    means = dimension_means(grades)
    return round(fmean(means.values()), 2) if means else 0.0
