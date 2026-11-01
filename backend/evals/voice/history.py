"""Every graded run, oldest first, so the voice's progress reads down one table per
workspace profile. Printed and written to ``runs/HISTORY.md``.

    uv run python -m evals.voice.history
"""

from __future__ import annotations

import json
import sys
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path

from .grading import (
    DIMENSIONS,
    GRADES_FILE,
    Dimension,
    dimension_means,
    expect_pass_rate,
    humor_tally,
    load_grades,
    voice_score,
)
from .record import RUN_FILE, load_run
from .review import FIRST_FILE, agreement
from .spec import KEY_FILE, RUNS_DIR, load_scenarios

HISTORY_FILE = "HISTORY.md"


@dataclass(frozen=True, slots=True)
class Row:
    started: str
    run: str
    model: str
    provider: str
    profile: str
    fingerprint: str
    voice: float
    means: dict[Dimension, float]
    expect: float
    #: Share of replies carrying a humorous line; None for grades from before the tally.
    humor: float | None
    #: Share of first-pass scores the review kept; None when the run wasn't reviewed.
    agreement: float | None


@dataclass(frozen=True, slots=True)
class History:
    rows: list[Row]
    ungraded: int
    stub: int


def collect_history(runs_dir: Path, quiet: Collection[str]) -> History:
    rows: list[Row] = []
    ungraded = stub = 0
    for run_dir in sorted(p.parent for p in runs_dir.glob(f"*/*/{RUN_FILE}")):
        run = load_run(run_dir)
        if run.stub:
            stub += 1
            continue
        if not (run_dir / GRADES_FILE).is_file():
            ungraded += 1
            continue
        grades = load_grades(run_dir)
        key = run_dir.parent / KEY_FILE
        models: dict[str, str] = json.loads(key.read_text()) if key.is_file() else {}
        tally = humor_tally(grades, run.replies(), quiet)
        first = run_dir / FIRST_FILE
        rows.append(
            Row(
                started=run.started_at,
                run=f"{run.group}/{run.name}",
                model=models.get(run.name, "?"),
                provider=run.provider or "?",
                profile=run.profile,
                fingerprint=run.prompt_fingerprint,
                voice=voice_score(grades),
                means=dimension_means(grades),
                expect=expect_pass_rate(grades),
                humor=tally.reply_rate if tally else None,
                agreement=agreement(load_grades(first), grades).rate if first.is_file() else None,
            )
        )
    rows.sort(key=lambda row: row.started)
    return History(rows, ungraded, stub)


def _pct(value: float | None) -> str:
    return "–" if value is None else f"{value:.0%}"


def render_history(history: History) -> str:
    out = ["# Voice eval history", ""]
    head = ["Started", "Run", "Model", "Provider", "Prompt", "Voice", *DIMENSIONS]
    head += ["Expect", "Humor/reply", "Agreement"]
    for profile in sorted({row.profile for row in history.rows}):
        out += [f"## Profile `{profile}`", "", f"| {' | '.join(head)} |"]
        out.append(f"|{'---|' * len(head)}")
        for row in (r for r in history.rows if r.profile == profile):
            means = [f"{row.means[d]:.2f}" if d in row.means else "–" for d in DIMENSIONS]
            cells = [row.started, row.run, row.model, row.provider, f"`{row.fingerprint}`"]
            cells += [f"{row.voice:.2f}", *means, _pct(row.expect), _pct(row.humor)]
            cells.append(_pct(row.agreement))
            out.append(f"| {' | '.join(cells)} |")
        out.append("")
    if not history.rows:
        out += ["No graded runs yet.", ""]
    out.append(f"Left out: {history.ungraded} ungraded, {history.stub} stub runs.")
    return "\n".join(out) + "\n"


def main(argv: list[str]) -> int:
    if argv:
        print(__doc__)
        return 2
    quiet = {s.id for s in load_scenarios() if s.quiet}
    text = render_history(collect_history(RUNS_DIR, quiet))
    _ = (RUNS_DIR / HISTORY_FILE).write_text(text)
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
