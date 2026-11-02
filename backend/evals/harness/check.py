"""Validate a harness run's ``grades.json`` against the schema and the scenarios it grades.

uv run python -m evals.harness.check <group>/<letter> [...]
"""

from __future__ import annotations

import sys
from collections.abc import Collection

from pydantic import ValidationError

from .backlog import read_backlog
from .grading import Grades, load_grades
from .record import Run, load_run
from .spec import Scenario, load_scenarios, run_path


def problems(
    grades: Grades, run: Run, scenarios: list[Scenario], backlog_ids: Collection[str] = ()
) -> list[str]:
    """Every way the grades disagree with what the run recorded, the scenarios ask and the
    backlog holds."""
    out: list[str] = []
    if grades.prompt_fingerprint != run.prompt_fingerprint:
        out.append("prompt_fingerprint does not match the run's")
    by_id = {s.id: s for s in scenarios}
    recorded = {(s.scenario, s.sample) for s in run.samples}
    graded: set[tuple[str, int]] = set()
    for scenario_grade in grades.scenarios:
        scenario = by_id.get(scenario_grade.id)
        if scenario is None:
            out.append(f"{scenario_grade.id}: not a scenario")
            continue
        wanted = set(scenario.graded)
        for sample in scenario_grade.samples:
            where = f"{scenario.id} #{sample.sample}"
            graded.add((scenario.id, sample.sample))
            if set(sample.dimensions) != wanted:
                out.append(f"{where}: dimensions {sorted(sample.dimensions)} != {sorted(wanted)}")
            if len(sample.expect) != len(scenario.expect):
                out.append(
                    f"{where}: {len(sample.expect)} expect results for {len(scenario.expect)} items"
                )
    for issue in grades.issues:
        if (issue.scenario, issue.sample) not in recorded:
            out.append(f"issue {issue.title!r}: {issue.scenario} #{issue.sample} wasn't recorded")
        if issue.kind == "behavior" and not (issue.gap.strip() and issue.principle):
            out.append(f"issue {issue.title!r}: a behavior issue needs `gap` and `principle`")
        if issue.recurs is not None and issue.recurs not in backlog_ids:
            out.append(f"issue {issue.title!r}: recurs names {issue.recurs}, not in the backlog")
    out += [f"{sid} #{n}: recorded but not graded" for sid, n in sorted(recorded - graded)]
    out += [f"{sid} #{n}: graded but not recorded" for sid, n in sorted(graded - recorded)]
    return out


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    scenarios = load_scenarios()
    backlog_ids = frozenset(e.id for e in read_backlog().entries)
    failed = False
    for raw in argv:
        run_dir = run_path(raw)
        try:
            grades = load_grades(run_dir)
        except (OSError, ValidationError) as exc:
            print(f"{run_dir}: {exc}")
            failed = True
            continue
        found = problems(grades, load_run(run_dir), scenarios, backlog_ids)
        failed = failed or bool(found)
        print(f"{run_dir}: {'ok' if not found else f'{len(found)} problems'}")
        for problem in found:
            print(f"  - {problem}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
