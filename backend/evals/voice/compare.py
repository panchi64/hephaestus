"""Graded runs side by side: every model in one run group, or two runs from anywhere.

    uv run python -m evals.voice.compare <group>              # the models of one group
    uv run python -m evals.voice.compare <group>/A <group>/B  # two runs, e.g. two prompts

Paths are run-group or run directories, absolute or relative to ``evals/voice/runs/``.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

from .grading import (
    DIMENSIONS,
    GRADES_FILE,
    Grades,
    ScenarioGrade,
    dimension_means,
    expect_pass_rate,
    load_grades,
    scenario_means,
    voice_score,
)
from .spec import KEY_FILE, RUNS_DIR

#: A scenario mean that moves this much between two runs is listed with its quotes.
MOVED = 1.0


@dataclass(frozen=True, slots=True)
class Graded:
    name: str
    grades: Grades


def _resolve(raw: str) -> Path:
    path = Path(raw)
    return path if path.is_absolute() or path.exists() else RUNS_DIR / raw


def _named(run_dir: Path) -> str:
    """``group/A (model)`` when the group's key names the model, else ``group/A``."""
    key = run_dir.parent / KEY_FILE
    models: dict[str, str] = json.loads(key.read_text()) if key.is_file() else {}
    label = f"{run_dir.parent.name}/{run_dir.name}"
    return f"{label} ({models[run_dir.name]})" if run_dir.name in models else label


def load_runs(args: list[str]) -> list[Graded]:
    if len(args) == 1:
        group = _resolve(args[0])
        dirs = sorted(d for d in group.iterdir() if (d / GRADES_FILE).is_file())
    else:
        dirs = [_resolve(arg) for arg in args]
    return [Graded(_named(d), load_grades(d)) for d in dirs]


def _fmt(value: float | None) -> str:
    return "–" if value is None else f"{value:.2f}"


def _delta(a: float | None, b: float | None) -> str:
    return "" if a is None or b is None else f"{b - a:+.2f}"


def _weakest(scenario: ScenarioGrade) -> str:
    """The lowest-scored dimension's evidence, the quote that explains the scenario's mean."""
    scored = [
        (score.score, dimension, score.evidence)
        for sample in scenario.samples
        for dimension, score in sample.dimensions.items()
    ]
    if not scored:
        return ""
    score, dimension, evidence = min(scored, key=lambda item: item[0])
    return f"{dimension} {score}: {evidence}" if evidence else f"{dimension} {score}"


def _warnings(runs: list[Graded], *, same_prompt_expected: bool) -> list[str]:
    out: list[str] = []
    sets = {frozenset(s.id for s in run.grades.scenarios) for run in runs}
    if len(sets) > 1:
        out.append("The runs graded different scenario sets; means are not like for like.")
    prints = {run.grades.prompt_fingerprint for run in runs}
    if same_prompt_expected and len(prints) > 1:
        out.append("The runs used different prompts; this compares models and prompts at once.")
    if not same_prompt_expected and len(prints) == 1:
        out.append("The runs used the same prompt; any difference is the model or sampling noise.")
    return out


def report(runs: list[Graded], *, same_prompt_expected: bool) -> str:
    names = [run.name for run in runs]
    pair = len(runs) == 2
    header = ["", *names, *(["Δ"] if pair else [])]
    rows: list[list[str]] = []
    means = [dimension_means(run.grades) for run in runs]
    for dimension in DIMENSIONS:
        values = [m.get(dimension) for m in means]
        if all(v is None for v in values):
            continue
        rows.append([f"`{dimension}`", *map(_fmt, values), *([_delta(*values)] if pair else [])])
    scores = [voice_score(run.grades) for run in runs]
    rates = [expect_pass_rate(run.grades) for run in runs]
    rows.append(["**voice score**", *map(_fmt, scores), *([_delta(*scores)] if pair else [])])
    rows.append(
        [
            "expect pass rate",
            *(f"{r:.0%}" for r in rates),
            *([f"{rates[1] - rates[0]:+.0%}"] if pair else []),
        ]
    )

    out = [f"# Voice comparison: {' vs '.join(names)}", ""]
    out += [
        f"> {warning}" for warning in _warnings(runs, same_prompt_expected=same_prompt_expected)
    ]
    out += ["", "| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    out += ["| " + " | ".join(row) + " |" for row in rows]

    per_scenario = [scenario_means(run.grades) for run in runs]
    ids = list(dict.fromkeys(i for m in per_scenario for i in m))
    out += [
        "",
        "## By scenario",
        "",
        "| scenario | " + " | ".join(names) + " |",
        "|" + "---|" * (len(names) + 1),
    ]
    out += [f"| {i} | " + " | ".join(_fmt(m.get(i)) for m in per_scenario) + " |" for i in ids]

    if pair:
        out += _movers(runs, per_scenario)
    return "\n".join(out) + "\n"


def _movers(runs: list[Graded], per_scenario: list[dict[str, float]]) -> list[str]:
    first, second = runs
    by_id = [{s.id: s for s in run.grades.scenarios} for run in runs]
    out = ["", f"## Moved by {MOVED:g} or more", ""]
    moved = [
        i
        for i in per_scenario[0]
        if i in per_scenario[1] and abs(per_scenario[1][i] - per_scenario[0][i]) >= MOVED
    ]
    for scenario_id in moved:
        before, after = (_fmt(means[scenario_id]) for means in per_scenario)
        out += [
            f"- **{scenario_id}** {before} → {after}",
            f"  - {first.name}: {_weakest(by_id[0][scenario_id])}",
            f"  - {second.name}: {_weakest(by_id[1][scenario_id])}",
        ]
    if not moved:
        out.append("None.")
    out += ["", "## Expect items that flipped", ""]
    flips: list[str] = []
    for scenario_id, grade in by_id[0].items():
        other = by_id[1].get(scenario_id)
        if other is None:
            continue
        pairs = zip(_expect_passes(grade), _expect_passes(other), strict=False)
        for index, (a, b) in enumerate(pairs, start=1):
            if a != b:
                flips.append(f"- {scenario_id} #{index}: {_verdict(a)} → {_verdict(b)}")
    return out + (flips or ["None."])


def _expect_passes(grade: ScenarioGrade) -> list[bool]:
    """Per expect item: whether every sample passed it."""
    count = len(grade.samples[0].expect)
    return [all(sample.expect[i].passed for sample in grade.samples) for i in range(count)]


def _verdict(passed: bool) -> str:
    return "pass" if passed else "fail"


def main(argv: list[str]) -> int:
    if len(argv) not in (1, 2):
        print(__doc__)
        return 2
    runs = load_runs(argv)
    if len(runs) < 2:
        print("Need at least two graded runs.")
        return 2
    print(report(runs, same_prompt_expected=len(argv) == 1))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
