"""History lists graded runs oldest first, per profile, and leaves out what it can't score."""

from __future__ import annotations

import json
from pathlib import Path

from evals.voice.grading import GRADES_FILE, Grades
from evals.voice.history import collect_history, render_history
from evals.voice.record import RUN_FILE, Run
from evals.voice.review import FIRST_FILE
from evals.voice.spec import KEY_FILE


def _grades(score: int) -> Grades:
    return Grades.model_validate(
        {
            "prompt_fingerprint": "f1",
            "scenarios": [
                {
                    "id": "s",
                    "samples": [
                        {
                            "sample": 1,
                            "dimensions": {"character": {"score": score, "evidence": "q"}},
                            "expect": [],
                        }
                    ],
                }
            ],
            "humor": [],
        }
    )


def _run(
    root: Path, group: str, started: str, *, grades: Grades | None, stub: bool = False
) -> Path:
    run_dir = root / group / "A"
    run_dir.mkdir(parents=True)
    run = Run(
        group=group,
        name="A",
        started_at=started,
        prompt_fingerprint="f1",
        stub=stub,
        provider="anthropic",
    )
    _ = (run_dir / RUN_FILE).write_text(run.model_dump_json())
    _ = (root / group / KEY_FILE).write_text(json.dumps({"A": f"model-{group}"}))
    if grades:
        _ = (run_dir / GRADES_FILE).write_text(grades.model_dump_json())
    return run_dir


def test_history_is_oldest_first_and_skips_what_it_cannot_score(tmp_path: Path) -> None:
    newer = _run(tmp_path, "b", "2026-11-02 09:00 UTC", grades=_grades(2))
    _ = (newer / FIRST_FILE).write_text(_grades(1).model_dump_json())
    _ = _run(tmp_path, "a", "2026-11-01 09:00 UTC", grades=_grades(3))
    _ = _run(tmp_path, "c", "2026-11-03 09:00 UTC", grades=None)
    _ = _run(tmp_path, "d", "2026-11-04 09:00 UTC", grades=_grades(3), stub=True)

    history = collect_history(tmp_path, quiet=())
    assert [row.run for row in history.rows] == ["a/A", "b/A"]
    assert [row.model for row in history.rows] == ["model-a", "model-b"]
    assert [row.agreement for row in history.rows] == [None, 0.0]
    assert (history.ungraded, history.stub) == (1, 1)
    text = render_history(history)
    assert "## Profile `bare`" in text
    assert "1 ungraded, 1 stub" in text
