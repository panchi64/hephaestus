"""Turn a graded run group's failed checks and grader issues into an issues list, and keep
the tracked backlog every run group feeds.

    uv run python -m evals.harness.issues <group>
    uv run python -m evals.harness.issues set H-004 fixed --note "fixed in a1b2c3d"

The first form writes ``runs/<group>/issues.md`` and merges the group into
``backlog.json``, re-rendering ``ISSUES.md``. A group recorded on the dev stub, or not yet
reviewed in full, writes its ``issues.md`` only: until a reviewer has classified them, a
failed scenario check may be the suite's fault rather than the product's. An issue already
in the backlog, matched by the grader's ``recurs`` or else by its title, counts another
occurrence, and one marked ``fixed`` that recurs is reopened as ``regressed``. A trial's
variant arms are left out: their failures were provoked on purpose.

``set`` also takes ``--gap TEXT`` and ``--principle NAME`` for a behavior issue's class.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import ValidationError

from evals.drive.timeline import redact_home

from .backlog import BACKLOG_PATH, STATUSES, Backlog, Entry, Status, Strict, read_backlog
from .grading import PRINCIPLES, SEVERITIES, Issue, IssueKind, Principle, Severity, load_grades
from .record import RUN_FILE, load_run
from .review import REPORT_FILE
from .spec import load_scenarios, run_path
from .variants import BASE, load_key

ISSUES_PATH = Path(__file__).with_name("ISSUES.md")
GROUP_FILE = "issues.md"
_EVIDENCE_CHARS = 300

_STATUS_NAMES: dict[str, Status] = {status: status for status in STATUSES}
_PRINCIPLE_NAMES: dict[str, Principle] = {principle: principle for principle in PRINCIPLES}


class Found(Strict):
    """One issue as a run group showed it, before it meets the backlog."""

    title: str
    kind: IssueKind
    severity: Severity
    area: str
    scenarios: list[str]
    samples: int = 1
    evidence: str
    suspect: str = ""
    gap: str = ""
    principle: Principle | None = None
    #: The backlog entry the grader named as the same failure.
    recurs: str | None = None
    #: ``check`` when a failed check filed it, ``grader`` when the reviewed grades did.
    source: Literal["check", "grader"]

    @property
    def fingerprint(self) -> str:
        return f"{self.kind}|{self.area}|{normalize(self.title)}"


def normalize(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()


def _trim(text: str) -> str:
    text = redact_home(" ".join(text.split()))
    return text if len(text) <= _EVIDENCE_CHARS else text[: _EVIDENCE_CHARS - 1] + "…"


def _more_severe(a: Severity, b: Severity) -> Severity:
    return a if SEVERITIES.index(a) <= SEVERITIES.index(b) else b


def _merge(found: Iterable[Found]) -> list[Found]:
    """One per fingerprint, keeping the worst severity and every scenario it showed in."""
    out: dict[str, Found] = {}
    for item in found:
        if (seen := out.get(item.fingerprint)) is None:
            out[item.fingerprint] = item.model_copy()
            continue
        seen.samples += item.samples
        seen.severity = _more_severe(seen.severity, item.severity)
        seen.scenarios = sorted({*seen.scenarios, *item.scenarios})
        seen.suspect = seen.suspect or item.suspect
        seen.gap = seen.gap or item.gap
        seen.principle = seen.principle or item.principle
        seen.recurs = seen.recurs or item.recurs
    return sorted(out.values(), key=lambda f: (SEVERITIES.index(f.severity), f.area, f.title))


_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")


@dataclass(frozen=True, slots=True)
class Collected:
    found: list[Found]
    #: Some run was recorded on the dev stub.
    stub: bool
    #: Every run has a reviewed grade.
    reviewed: bool
    #: The day the group's first run started (`YYYY-MM-DD`), or None for a group with no run.
    ran_on: str | None = None


def collect(group_dir: Path) -> Collected:
    """Every issue a group's runs show, deduplicated."""
    areas = {s.id: s.area for s in load_scenarios()}
    found: list[Found] = []
    stub = False
    reviewed = True
    started: list[str] = []
    key = load_key(group_dir)
    for run_dir in sorted(p for p in group_dir.iterdir() if (p / RUN_FILE).is_file()):
        if (letter := key.get(run_dir.name)) is not None and letter.arm != BASE:
            continue
        run = load_run(run_dir)
        stub = stub or run.stub
        if day := _DAY.match(run.started_at):
            started.append(day.group())
        # A reviewed grader files a failed scenario check under the kind it really is (an
        # eval fault, the agent's, the product's); unreviewed, the check stands for itself.
        graded = (run_dir / REPORT_FILE).is_file()
        reviewed = reviewed and graded
        for sample in run.samples:
            area = areas.get(sample.scenario, "unknown")
            for check in sample.failed_checks():
                if graded and not check.implicit:
                    continue
                label = f"{sample.scenario}: {check.label}" if not check.implicit else check.label
                found.append(
                    Found(
                        title=f"Check failed: {label}",
                        kind=check.kind,
                        severity="major" if check.kind == "platform" else "minor",
                        area=area,
                        scenarios=[sample.scenario],
                        evidence=_trim(check.detail or check.label),
                        source="check",
                    )
                )
        if graded:
            found += [_from_grader(issue, areas) for issue in load_grades(run_dir).issues]
    return Collected(
        _merge(found), stub=stub, reviewed=reviewed, ran_on=min(started) if started else None
    )


def _from_grader(issue: Issue, areas: dict[str, str]) -> Found:
    return Found(
        title=issue.title,
        kind=issue.kind,
        severity=issue.severity,
        area=areas.get(issue.scenario, "unknown"),
        scenarios=[issue.scenario],
        evidence=_trim(issue.evidence),
        suspect=issue.suspect,
        gap=issue.gap,
        principle=issue.principle,
        recurs=issue.recurs,
        source="grader",
    )


def merge_backlog(backlog: Backlog, found: list[Found], group: str, seen: str) -> list[str]:
    """Fold a group's issues into the backlog; returns the ids it touched, none for a
    group already merged."""
    if group in backlog.groups:
        return []
    backlog.groups.append(group)
    by_print = {e.fingerprint: e for e in backlog.entries}
    by_id = {e.id: e for e in backlog.entries}
    touched: list[str] = []
    for item in found:
        # The grader's `recurs` wins over the title: a recurrence is rarely worded the same.
        named = by_id.get(item.recurs) if item.recurs else None
        entry = named or by_print.get(item.fingerprint)
        if entry is None:
            number = 1 + max((int(e.id.removeprefix("H-")) for e in backlog.entries), default=0)
            entry = Entry(
                id=f"H-{number:03d}",
                fingerprint=item.fingerprint,
                title=item.title,
                kind=item.kind,
                severity=item.severity,
                area=item.area,
                scenarios=item.scenarios,
                first_seen=seen,
                last_seen=seen,
                occurrences=item.samples,
                evidence=item.evidence,
                suspect=item.suspect,
                gap=item.gap,
                principle=item.principle,
            )
            backlog.entries.append(entry)
            by_print[entry.fingerprint] = entry
            by_id[entry.id] = entry
        else:
            entry.occurrences += item.samples
            entry.last_seen = seen
            entry.severity = _more_severe(entry.severity, item.severity)
            entry.scenarios = sorted({*entry.scenarios, *item.scenarios})
            entry.evidence = item.evidence
            entry.suspect = entry.suspect or item.suspect
            entry.gap = entry.gap or item.gap
            entry.principle = entry.principle or item.principle
            if entry.status == "fixed":
                entry.status = "regressed"
                fix = f" The fix was: {entry.note}" if entry.note else ""
                entry.note = f"Recurred after a fix in {group} as {item.title!r}.{fix}"
        touched.append(entry.id)
    return touched


def _row(*cells: str) -> str:
    return f"| {' | '.join(cells)} |"


def render_group(group: str, found: list[Found], stub: bool) -> str:
    out = [f"# Issues: {group}", ""]
    if stub:
        out += ["*Recorded on the dev stub: plumbing only, not merged into the backlog.*", ""]
    if not found:
        return "\n".join([*out, "None found.", ""])
    out += [_row("Severity", "Kind", "Area", "Issue", "Scenarios", "From"), _row(*["---"] * 6)]
    out += [
        _row(f.severity, f.kind, f.area, f.title, ", ".join(f.scenarios), f.source) for f in found
    ]
    out += ["", "## Evidence", ""]
    out += [
        f"- **{f.title}**: {f.evidence}{_reasoning(f.principle, f.gap, f.recurs)}" for f in found
    ]
    return "\n".join([*out, ""])


def _reasoning(principle: Principle | None, gap: str, recurs: str | None = None) -> str:
    """The class a behavior issue belongs to, as an evidence line's tail."""
    parts = [f"*Principle: {principle}.*"] if principle else []
    parts += [f"*Gap: {gap}*"] if gap else []
    parts += [f"*Recurs: {recurs}.*"] if recurs else []
    return "".join(f" {part}" for part in parts)


def render_backlog(backlog: Backlog) -> str:
    out = [
        "# Harness issues backlog",
        "",
        "Generated from `backlog.json` by `python -m evals.harness.issues`; edit status with",
        "`issues set`, not by hand.",
        "",
    ]
    for status in STATUSES:
        entries = [e for e in backlog.entries if e.status == status]
        if not entries:
            continue
        entries.sort(key=lambda e: (SEVERITIES.index(e.severity), e.id))
        out += [f"## {status.capitalize()}", ""]
        out += [_row("Id", "Severity", "Kind", "Area", "Issue", "Seen", "Suspect")]
        out += [_row(*["---"] * 7)]
        for e in entries:
            seen = f"×{e.occurrences}, {e.first_seen} → {e.last_seen}"
            out.append(_row(e.id, e.severity, e.kind, e.area, e.title, seen, e.suspect))
        out.append("")
        out += [
            f"- **{e.id}**: {e.evidence}{_reasoning(e.principle, e.gap)}"
            + (f" *Note: {e.note}*" if e.note else "")
            for e in entries
        ]
        out.append("")
    return "\n".join(out)


def load_backlog() -> Backlog:
    return read_backlog(BACKLOG_PATH)


def save_backlog(backlog: Backlog) -> None:
    _ = BACKLOG_PATH.write_text(backlog.model_dump_json(indent=2) + "\n")
    _ = ISSUES_PATH.write_text(render_backlog(backlog))


def _set(argv: list[str]) -> int:
    status = _STATUS_NAMES.get(argv[1]) if len(argv) > 1 else None
    flags = dict(zip(argv[2::2], argv[3::2], strict=False))
    principle = _PRINCIPLE_NAMES.get(flags.get("--principle", ""))
    if (
        status is None
        or len(argv) % 2
        or set(flags) - {"--note", "--gap", "--principle"}
        or ("--principle" in flags and principle is None)
    ):
        print(
            f"usage: issues set H-### {{{','.join(STATUSES)}}} [--note TEXT] [--gap TEXT] "
            + f"[--principle {{{','.join(PRINCIPLES)}}}]"
        )
        return 2
    issue_id = argv[0]
    backlog = load_backlog()
    entry = next((e for e in backlog.entries if e.id == issue_id), None)
    if entry is None:
        print(f"No {issue_id} in the backlog.")
        return 2
    entry.status = status
    entry.note = flags.get("--note", entry.note)
    entry.gap = flags.get("--gap", entry.gap)
    entry.principle = principle or entry.principle
    save_backlog(backlog)
    print(f"{issue_id} → {status}")
    return 0


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    if argv[0] == "set":
        return _set(argv[1:])
    group_dir = run_path(argv[0])
    try:
        collected = collect(group_dir)
    except (OSError, ValidationError) as exc:
        print(f"{group_dir}: {exc}")
        return 1
    found = collected.found
    rendered = render_group(group_dir.name, found, stub=collected.stub)
    _ = (group_dir / GROUP_FILE).write_text(rendered)
    print(f"wrote {group_dir / GROUP_FILE}: {len(found)} issues")
    if collected.stub:
        print("A stub run; the backlog is left as it is.")
        return 0
    if not collected.reviewed:
        print("Not every run is reviewed yet; the backlog is left as it is.")
        return 0
    backlog = load_backlog()
    group = group_dir.name
    if group in backlog.groups:
        print(f"{group} was merged already; the backlog is left as it is.")
        return 0
    ran_on = collected.ran_on or f"{datetime.now(UTC):%Y-%m-%d}"
    touched = merge_backlog(backlog, found, group, f"{group} ({ran_on})")
    save_backlog(backlog)
    print(f"backlog: {len(touched)} issues merged into {BACKLOG_PATH.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
