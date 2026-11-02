"""The harness suite's files, grading, review and issues backlog — offline."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from evals.drive.timeline import Text, Turn
from evals.harness import issues
from evals.harness.backlog import Backlog
from evals.harness.check import problems
from evals.harness.document import render
from evals.harness.grading import DIMENSIONS, Grades, Issue
from evals.harness.implicit import CheckResult
from evals.harness.issues import Found, merge_backlog, render_backlog
from evals.harness.record import Run, Sample
from evals.harness.review import Review, apply_review
from evals.harness.spec import (
    FIXTURES_DIR,
    PROJECT_TOKEN,
    Scenario,
    StubReply,
    load_scenarios,
)

_GRADING = Path(__file__).parent.parent / "evals" / "harness" / "GRADING.md"


def _scenario(**fields: object) -> Scenario:
    base: dict[str, object] = {
        "id": "s",
        "area": "main",
        "about": "a",
        "prompts": ["p"],
        "dimensions": ["tool_use"],
        "expect": ["e1", "e2"],
    }
    return Scenario.model_validate({**base, **fields})


def test_the_shipped_scenarios_load_and_cover_every_area() -> None:
    scenarios = load_scenarios()
    assert len(scenarios) >= 28
    assert {s.area for s in scenarios} == {
        "main",
        "tools",
        "delegation",
        "intercom",
        "subagent",
        "models",
        "solving",
        "flow",
    }
    assert all((FIXTURES_DIR / s.fixture).is_dir() for s in scenarios if s.fixture)
    assert any(s.stub for s in scenarios), "the stub plumbing run needs scripted scenarios"


def test_grading_md_defines_every_dimension() -> None:
    defined = re.findall(r"^### `(\w+)`", _GRADING.read_text(), flags=re.MULTILINE)
    assert tuple(defined) == DIMENSIONS


@pytest.mark.parametrize(
    "fields",
    [
        {"prompts": [f"look at {PROJECT_TOKEN}"]},
        {"git": True},
        {"dimensions": ["outcome"]},
        {"actions": [{"at": "first_tool", "do": "steer"}]},
        {"actions": [{"at": "whenever", "do": "cancel"}]},
        {"actions": [{"at": "first_tool", "do": "cancel", "turn": 2}]},
    ],
)
def test_inconsistent_scenarios_are_refused(fields: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _ = _scenario(**fields)


def test_a_stub_reply_fills_tokens_and_numbers_its_calls() -> None:
    reply = StubReply.model_validate(
        {
            "match": "look at {project}",
            "tool_calls": [
                {"name": "agents_spawn", "arguments": {"model": "{local}", "task": "{project}"}},
                {"name": "agents_models"},
            ],
        }
    )
    wire = reply.wire({PROJECT_TOKEN: "~/projects/p", "{local}": "stub-local/m"}, 3)
    calls = wire["tool_calls"]
    assert isinstance(calls, list)
    assert [c["id"] for c in calls] == ["call_3_0", "call_3_1"]
    assert json.loads(calls[0]["arguments"]) == {"model": "stub-local/m", "task": "~/projects/p"}
    assert wire["match"] == "look at ~/projects/p"


def _run(scenario: Scenario, *checks: CheckResult) -> Run:
    turn = Turn(prompt="p", segments=[Text(text="Done.")], ended="done")
    sample = Sample(scenario=scenario.id, sample=1, turns=[turn], checks=list(checks))
    return Run(
        group="g", name="A", started_at="now", prompt_fingerprint="f", stub=False, samples=[sample]
    )


def _grades(**fields: object) -> Grades:
    base: dict[str, object] = {
        "prompt_fingerprint": "f",
        "scenarios": [
            {
                "id": "s",
                "samples": [
                    {
                        "sample": 1,
                        "dimensions": {
                            "outcome": {"score": 3},
                            "tool_use": {"score": 2, "evidence": "two reads"},
                            "operator_flow": {"score": 3},
                        },
                        "expect": [{"passed": True}, {"passed": False, "note": "n"}],
                    }
                ],
            }
        ],
        "issues": [
            {
                "title": "Reads the file twice",
                "kind": "behavior",
                "severity": "minor",
                "scenario": "s",
                "sample": 1,
                "evidence": "files_read_file ×2",
                "gap": "re-read what was already in context",
                "principle": "cost",
            }
        ],
    }
    return Grades.model_validate({**base, **fields})


def test_grades_that_match_the_run_pass_the_check() -> None:
    scenario = _scenario()
    assert problems(_grades(), _run(scenario), [scenario]) == []


def test_the_check_names_missing_dimensions_and_stray_issues() -> None:
    scenario = _scenario(dimensions=["tool_use", "solving"])
    grades = _grades(
        issues=[
            {
                "title": "t",
                "kind": "ux",
                "severity": "polish",
                "scenario": "s",
                "sample": 4,
                "evidence": "e",
            }
        ]
    )
    found = problems(grades, _run(scenario), [scenario])
    assert any("dimensions" in p for p in found)
    assert any("wasn't recorded" in p for p in found)


def test_a_behavior_issue_names_its_class_and_any_recurrence_exists() -> None:
    scenario = _scenario()
    issue: dict[str, object] = {
        "title": "t",
        "kind": "behavior",
        "severity": "minor",
        "scenario": "s",
        "sample": 1,
        "evidence": "e",
        "recurs": "H-009",
    }

    found = problems(_grades(issues=[issue]), _run(scenario), [scenario], frozenset({"H-001"}))

    assert any("needs `gap` and `principle`" in p for p in found)
    assert any("recurs names H-009" in p for p in found)


def test_a_score_below_three_needs_evidence() -> None:
    with pytest.raises(ValidationError):
        _ = Grades.model_validate(
            {
                "prompt_fingerprint": "f",
                "scenarios": [
                    {
                        "id": "s",
                        "samples": [
                            {"sample": 1, "dimensions": {"outcome": {"score": 1}}, "expect": []}
                        ],
                    }
                ],
            }
        )


def test_a_review_corrects_scores_expects_and_issues() -> None:
    review = Review.model_validate(
        {
            "reviewer": "r",
            "dimensions": [
                {
                    "scenario": "s",
                    "sample": 1,
                    "dimension": "tool_use",
                    "score": 1,
                    "evidence": "e",
                    "reason": "r",
                }
            ],
            "expect": [{"scenario": "s", "sample": 1, "item": 2, "passed": True, "reason": "r"}],
            "issues_changed": [
                {
                    "was": "Reads the file twice",
                    "reason": "it's a platform fault",
                    "issue": {
                        "title": "Read tool returns a stale copy",
                        "kind": "platform",
                        "severity": "major",
                        "scenario": "s",
                        "sample": 1,
                        "evidence": "e",
                    },
                }
            ],
        }
    )
    final = apply_review(_grades(), review)
    sample = final.scenarios[0].samples[0]
    assert sample.dimensions["tool_use"].score == 1
    assert sample.expect[1].passed
    assert [(i.title, i.kind) for i in final.issues] == [
        ("Read tool returns a stale copy", "platform")
    ]


def test_a_review_naming_an_issue_that_isnt_there_is_refused() -> None:
    review = Review.model_validate(
        {
            "reviewer": "r",
            "issues_removed": [
                {"scenario": "s", "sample": 1, "title": "No such issue", "reason": "r"}
            ],
        }
    )
    with pytest.raises(ValueError, match="No such issue"):
        _ = apply_review(_grades(), review)


def _found(title: str = "Steer ignored", **fields: object) -> Found:
    base: dict[str, object] = {
        "title": title,
        "kind": "behavior",
        "severity": "minor",
        "area": "flow",
        "scenarios": ["flow-steer"],
        "evidence": "e",
        "source": "grader",
    }
    return Found.model_validate({**base, **fields})


def test_the_backlog_counts_recurrences_and_reopens_a_fixed_issue() -> None:
    backlog = Backlog()
    assert merge_backlog(backlog, [_found()], "g1", "g1") == ["H-001"]
    again = [_found("Steer  ignored!", severity="major")]
    assert merge_backlog(backlog, again, "g2", "g2") == ["H-001"]
    assert merge_backlog(backlog, again, "g2", "g2") == []
    entry = backlog.entries[0]
    assert (entry.occurrences, entry.severity, entry.last_seen) == (2, "major", "g2")
    entry.status = "fixed"
    both = [_found(), _found("Card stuck live", kind="platform")]
    _ = merge_backlog(backlog, both, "g3", "g3")
    assert [(e.id, e.status) for e in backlog.entries] == [
        ("H-001", "regressed"),
        ("H-002", "open"),
    ]
    assert "## Regressed" in render_backlog(backlog)


def test_a_named_recurrence_reopens_its_entry_whatever_its_title() -> None:
    backlog = Backlog()
    _ = merge_backlog(backlog, [_found("No task list for a four-step job")], "g1", "g1")
    backlog.entries[0].status = "fixed"
    backlog.entries[0].note = "Instructions changed."
    reworded = _found(
        "Three-part job done without a task list",
        kind="behavior",
        recurs="H-001",
        principle="visibility",
        gap="didn't make multi-part work visible while the operator waited",
    )

    assert merge_backlog(backlog, [reworded], "g2", "g2") == ["H-001"]
    (entry,) = backlog.entries
    assert (entry.status, entry.occurrences, entry.principle) == ("regressed", 2, "visibility")
    assert entry.note == (
        "Recurred after a fix in g2 as 'Three-part job done without a task list'. "
        + "The fix was: Instructions changed."
    )
    assert "*Principle: visibility.*" in render_backlog(backlog)


def test_a_group_is_dated_by_its_first_run_not_by_when_it_is_filed(tmp_path: Path) -> None:
    for name, started in (("A", "2026-11-01 21:25 UTC"), ("B", "2026-11-02 00:10 UTC")):
        run = _run(_scenario()).model_copy(update={"name": name, "started_at": started})
        run_dir = tmp_path / "g" / name
        run_dir.mkdir(parents=True)
        _ = (run_dir / "run.json").write_text(run.model_dump_json())

    assert issues.collect(tmp_path / "g").ran_on == "2026-11-01"


def test_a_reviewed_run_files_its_grader_issues_in_place_of_scenario_checks(
    tmp_path: Path,
) -> None:
    scenario = _scenario()
    run = _run(
        scenario,
        CheckResult(label="answer matches /x/", passed=False),
        CheckResult(label="no run error", passed=False, implicit=True, kind="platform"),
    )
    run_dir = tmp_path / "g" / "A"
    run_dir.mkdir(parents=True)
    _ = (run_dir / "run.json").write_text(run.model_dump_json())

    unreviewed = issues.collect(tmp_path / "g")
    assert not unreviewed.reviewed
    assert {f.title for f in unreviewed.found} == {
        "Check failed: s: answer matches /x/",
        "Check failed: no run error",
    }

    _ = (run_dir / "grades.json").write_text(_grades().model_dump_json())
    _ = (run_dir / "review.md").write_text("# Review\n")
    reviewed = issues.collect(tmp_path / "g")
    assert reviewed.reviewed
    assert {f.title for f in reviewed.found} == {
        "Check failed: no run error",
        "Reads the file twice",
    }


def test_the_set_command_edits_status(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(issues, "BACKLOG_PATH", tmp_path / "backlog.json")
    monkeypatch.setattr(issues, "ISSUES_PATH", tmp_path / "ISSUES.md")
    backlog = Backlog()
    _ = merge_backlog(backlog, [_found()], "g1", "g1")
    issues.save_backlog(backlog)
    assert issues.main(["set", "H-001", "fixed", "--note", "done in abc"]) == 0
    saved = issues.load_backlog().entries[0]
    assert (saved.status, saved.note) == ("fixed", "done in abc")
    assert "done in abc" in (tmp_path / "ISSUES.md").read_text()
    assert issues.main(["set", "H-001", "bogus"]) == 2
    assert issues.main(["set", "H-001", "fixed", "--principle", "vibes"]) == 2
    classed = ["--gap", "skipped a check", "--principle", "state"]
    assert issues.main(["set", "H-001", "open", *classed]) == 0
    saved = issues.load_backlog().entries[0]
    assert (saved.status, saved.note, saved.gap, saved.principle) == (
        "open",
        "done in abc",
        "skipped a check",
        "state",
    )


def test_the_transcript_shows_checks_and_sub_agents() -> None:
    scenario = _scenario()
    run = _run(
        scenario,
        CheckResult(label="every turn ended done", passed=False, implicit=True, kind="platform"),
    )
    text = render(run, [scenario])
    assert "**FAIL** (platform) | every turn ended done *(implicit)*" in text
    assert "GRADING.md" in text


def test_an_issue_needs_evidence() -> None:
    with pytest.raises(ValidationError):
        _ = Issue.model_validate(
            {
                "title": "t",
                "kind": "ux",
                "severity": "minor",
                "scenario": "s",
                "sample": 1,
                "evidence": "",
            }
        )
