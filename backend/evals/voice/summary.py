"""Write a graded run's ``grades.md`` from its ``grades.json``, so every summary has the
same shape whoever graded it.

    uv run python -m evals.voice.summary <group>/<letter> [...]
"""

from __future__ import annotations

import sys
from collections.abc import Collection
from pathlib import Path

from .grading import (
    Grades,
    dimension_means,
    expect_pass_rate,
    humor_tally,
    load_grades,
    voice_score,
)
from .record import Run, load_run
from .spec import load_scenarios, run_path

SUMMARY_FILE = "grades.md"


def render_summary(grades: Grades, run: Run, quiet: Collection[str]) -> str:
    out = [
        f"# Grades: {run.group} / {run.name}",
        "",
        f"- grader: {grades.grader or 'unknown'}",
        f"- prompt fingerprint: `{grades.prompt_fingerprint}`",
        f"- workspace profile: `{run.profile}`",
        "",
        f"**Voice score: {voice_score(grades):.2f} / 3**",
        f"Expect pass rate: {expect_pass_rate(grades):.0%}",
        "",
        "| Dimension | Mean |",
        "|---|---|",
        *(f"| `{d}` | {mean:.2f} |" for d, mean in dimension_means(grades).items()),
        "",
        "## Humor",
        "",
    ]
    tally = humor_tally(grades, run.replies(), quiet)
    if tally is None:
        out.append("Graded before the humor tally existed.")
    else:
        faults = ", ".join(f"{f} {n}" for f, n in tally.faults.items() if n) or "none"
        out += [
            f"- lines: {tally.lines}, in {tally.reply_rate:.0%} of replies",
            f"- conversations with a line: {tally.conversation_rate:.0%}",
            f"- devices used: {tally.devices}; back to back: {tally.back_to_back}",
            f"- faults: {faults}",
        ]
    out += ["", "## Worst replies", ""]
    out += [
        f'{i}. `{w.scenario}` #{w.sample}: "{w.quote}" ({w.reason})'
        for i, w in enumerate(grades.worst, 1)
    ] or ["None listed."]
    out += ["", "## Patterns", "", grades.notes or "None noted.", ""]
    return "\n".join(out)


def write_summary(run_dir: Path, quiet: Collection[str]) -> None:
    text = render_summary(load_grades(run_dir), load_run(run_dir), quiet)
    _ = (run_dir / SUMMARY_FILE).write_text(text)


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    quiet = {s.id for s in load_scenarios() if s.quiet}
    for raw in argv:
        run_dir = run_path(raw)
        write_summary(run_dir, quiet)
        print(f"wrote {run_dir / SUMMARY_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
