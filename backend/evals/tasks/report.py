"""The markdown summary beside a results file: outcomes by area and why tasks fell short."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from .results import TaskResult


def _causes(result: TaskResult) -> list[str]:
    """Every platform-visible reason the task may have fallen short, worst first."""
    causes: list[str] = []
    for turn in result.turns:
        if turn.ended != "done":
            causes.append(f"ended {turn.ended}")
        causes.extend(f"limit {limit}" for limit in turn.limits)
        if turn.tool_failures:
            causes.append("tool failures")
    if result.outcome != "pass" and not causes:
        causes.append("finished but checks failed")
    return list(dict.fromkeys(causes))


def write_summary(results: list[TaskResult], *, model: str, path: Path) -> Path:
    scored = [result for result in results if result.outcome != "skipped"]
    outcomes = Counter(result.outcome for result in scored)
    tally = ", ".join(f"{outcomes[kind]} {kind}" for kind in ("pass", "partial", "fail"))
    columns = (
        "Task",
        "Area",
        "Outcome",
        "Steps",
        "Tool failures",
        "Approvals",
        "Max first token (s)",
        "Wall (s)",
        "Why",
    )
    lines = [
        f"# Task suite: {model}",
        "",
        f"{tally} of {len(scored)} run ({len(results) - len(scored)} skipped).",
        "",
        "| " + " | ".join(columns) + " |",
        "|" + "---|" * len(columns),
    ]
    for result in results:
        steps = sum(turn.steps for turn in result.turns)
        failures = sum(len(turn.tool_failures) for turn in result.turns)
        approvals = sum(turn.approvals for turn in result.turns)
        first = max((t for turn in result.turns for t in turn.first_token_s), default=0.0)
        wall = sum(turn.wall_s for turn in result.turns)
        why = result.reason or ", ".join(
            label for label, passed in result.checks.items() if not passed
        )
        cells = (
            result.id,
            result.area,
            result.outcome,
            str(steps),
            str(failures),
            str(approvals),
            f"{first:.1f}",
            f"{wall:.0f}",
            why,
        )
        lines.append("| " + " | ".join(cells) + " |")
    cause_counts = Counter(cause for result in scored for cause in _causes(result))
    if cause_counts:
        lines += ["", "## Causes", "", "| Cause | Tasks |", "|---|---|"]
        lines += [f"| {cause} | {count} |" for cause, count in cause_counts.most_common()]
    _ = path.write_text("\n".join(lines) + "\n")
    return path
