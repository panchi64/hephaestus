"""The task suite's file and checks — offline, so a malformed task fails here, not mid-run."""

from __future__ import annotations

from pathlib import Path

import pytest

from evals.tasks.report import write_summary
from evals.tasks.results import TaskResult, TurnRecord
from evals.tasks.spec import (
    AnswerContains,
    Evidence,
    FileContains,
    FileExists,
    ToolUsed,
    load_tasks,
)


def test_the_shipped_task_file_loads_with_unique_ids() -> None:
    tasks = load_tasks()
    assert len(tasks) >= 15
    assert len({task.id for task in tasks}) == len(tasks)


def test_duplicate_ids_are_refused(tmp_path: Path) -> None:
    path = tmp_path / "tasks.toml"
    task = '[[task]]\nid = "a"\narea = "build"\nprompts = ["x"]\n'
    _ = path.write_text(task + task)
    with pytest.raises(ValueError, match="duplicate task ids"):
        _ = load_tasks(path)


def test_checks_read_the_answer_the_tools_and_the_sandbox(tmp_path: Path) -> None:
    _ = (tmp_path / "primes.txt").write_text("2\n3\n97\n")
    evidence = Evidence(
        answer="There are 25 primes.", tools_completed=["code_execute"], workdir=tmp_path
    )
    assert AnswerContains(text=["25 PRIMES"]).passes(evidence)
    assert not AnswerContains(text=["25", "missing"]).passes(evidence)
    assert AnswerContains(text=["25", "missing"], mode="any").passes(evidence)
    assert FileExists(path="primes.txt").passes(evidence)
    assert not FileExists(path="absent.txt").passes(evidence)
    assert FileContains(path="primes.txt", text=["97"]).passes(evidence)
    assert not FileContains(path="absent.txt", text=["97"]).passes(evidence)
    assert ToolUsed(prefix="code_").passes(evidence)
    assert not ToolUsed(prefix="web_").passes(evidence)


def test_the_summary_counts_outcomes_and_names_causes(tmp_path: Path) -> None:
    results = [
        TaskResult(id="ok", area="build", outcome="pass", turns=[TurnRecord(ended="done")]),
        TaskResult(
            id="stopped",
            area="build",
            outcome="fail",
            turns=[TurnRecord(ended="blocked", limits=["steps"])],
        ),
        TaskResult(id="skip", area="research", outcome="skipped"),
    ]
    text = write_summary(results, model="m", path=tmp_path / "s.md").read_text()
    assert "1 pass, 0 partial, 1 fail of 2 run (1 skipped)." in text
    assert "| ended blocked | 1 |" in text
    assert "| limit steps | 1 |" in text
