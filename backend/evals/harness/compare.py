"""The arms of a trial side by side: ``uv run python -m evals.harness.compare <group>``.

For each scenario and arm: the share of checks that passed, the mean grader score once the
group is graded, and the issues filed. Above that, whether each variant arm's edit landed: a
removed sentence must reach a base arm and never the variant, and an added one must reach
the variant. An arm whose edit didn't land tested nothing and is marked invalid.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from statistics import fmean

from .grading import GRADES_FILE, Grades, load_grades
from .record import RUN_FILE, Run, load_run
from .spec import RUNS_DIR
from .variants import BASE, ArmKey, load_key


@dataclass(frozen=True, slots=True)
class Arm:
    letter: str
    key: ArmKey
    run: Run
    grades: Grades | None

    @property
    def label(self) -> str:
        return f"{self.key.arm} ({self.letter})"


def load_arms(group_dir: Path) -> list[Arm]:
    key = load_key(group_dir)
    arms: list[Arm] = []
    for run_dir in sorted(p for p in group_dir.iterdir() if (p / RUN_FILE).is_file()):
        graded = (run_dir / GRADES_FILE).is_file()
        grades = load_grades(run_dir) if graded else None
        letter = key.get(run_dir.name, ArmKey())
        arms.append(Arm(run_dir.name, letter, load_run(run_dir), grades))
    return arms


def _seen(run: Run, text: str) -> int:
    """How many samples carried ``text`` in at least one request."""
    return sum(any(text in r.seen for r in sample.requests) for sample in run.samples)


def landing(arms: list[Arm]) -> dict[str, list[str]]:
    """Per variant arm, why its edit didn't land; an empty list means it did."""
    bases = [arm.run for arm in arms if arm.key.arm == BASE]
    problems: dict[str, list[str]] = {}
    for arm in arms:
        if arm.key.arm == BASE:
            continue
        found: list[str] = []
        for probe in arm.key.probes:
            short = probe.text if len(probe.text) <= 60 else f"{probe.text[:57]}…"
            if probe.present and not _seen(arm.run, probe.text):
                found.append(f"added text never sent: {short!r}")
            if not probe.present:
                if _seen(arm.run, probe.text):
                    found.append(f"removed text still sent: {short!r}")
                if not any(_seen(base, probe.text) for base in bases):
                    found.append(f"no base arm sent the removed text: {short!r}")
        problems[arm.label] = found
    return problems


def _check_rate(run: Run, scenario: str) -> str:
    results = [c.passed for s in run.samples if s.scenario == scenario for c in s.checks]
    return f"{sum(results)}/{len(results)}" if results else "–"


def _score(grades: Grades | None, scenario: str) -> str:
    if grades is None:
        return "ungraded"
    scores = [
        d.score
        for s in grades.scenarios
        if s.id == scenario
        for sample in s.samples
        for d in sample.dimensions.values()
    ]
    return f"{fmean(scores):.2f}" if scores else "–"


def _issues(grades: Grades | None, scenario: str) -> str:
    if grades is None:
        return "–"
    found = [i.recurs or i.title for i in grades.issues if i.scenario == scenario]
    return ", ".join(found) or "none"


def report(arms: list[Arm]) -> str:
    lines: list[str] = []
    for label, problems in landing(arms).items():
        verdict = "landed" if not problems else "INVALID: " + "; ".join(problems)
        lines.append(f"- {label}: {verdict}")
    scenarios = list(dict.fromkeys(s.scenario for arm in arms for s in arm.run.samples))
    header = "| Scenario | Arm | Checks passed | Mean score | Issues |"
    lines += ["", header, "|---|---|---|---|---|"]
    for scenario in scenarios:
        for arm in arms:
            cells = [
                scenario,
                arm.label,
                _check_rate(arm.run, scenario),
                _score(arm.grades, scenario),
                _issues(arm.grades, scenario),
            ]
            lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: python -m evals.harness.compare <group>")
        return 2
    group_dir = RUNS_DIR / argv[0]
    if not group_dir.is_dir():
        print(f"no run group at {group_dir}")
        return 2
    print(report(load_arms(group_dir)), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
