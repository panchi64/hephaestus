"""The checks every sample gets, whatever it probes: the platform failing underneath it (a
run that errored, a tool the platform broke, a sub-agent left running or ended without a
report), and the agent leaving work hanging."""

from __future__ import annotations

import re
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict

from evals.drive.timeline import Error
from harness.manifests._subagent_wake import NO_REPORT

from .evidence import Evidence

type CheckKind = Literal["platform", "behavior"]

_READ = "agents_read"
_PAGED = re.compile(r'"start":\s*[1-9]')


class CheckResult(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)

    label: str
    passed: bool
    #: Every sample gets the implicit checks; a scenario lists the rest.
    implicit: bool = False
    #: What a failure is filed as.
    kind: CheckKind = "behavior"
    detail: str = ""


def _implicit(label: str, failures: list[str], kind: CheckKind = "platform") -> CheckResult:
    return CheckResult(
        label=label, passed=not failures, implicit=True, kind=kind, detail="; ".join(failures)
    )


def implicit(evidence: Evidence) -> list[CheckResult]:
    parent = evidence.parent_turns()
    every = [*parent, *evidence.child_turns()]
    unfinished = [
        f"turn {i} ended {t.ended}"
        for i, t in enumerate(evidence.turns, start=1)
        if t.ended != "done" and i - 1 not in evidence.cancelled
    ] + [
        f"new-thread turn {i} ended {t.ended}"
        for i, t in enumerate(evidence.later, start=1)
        if t.ended != "done"
    ]
    errors = [s.message for t in every for s in t.segments if isinstance(s, Error)]
    broken = [f"{tool.name}: {tool.outcome}" for t in every for tool in t.tools() if tool.platform]
    running = [a.handle for a in evidence.subagents if a.status == "running"]
    waiting = [a.handle for a in evidence.subagents if a.status == "blocked"]
    silent = [a.handle for a in evidence.subagents if a.error == NO_REPORT]
    # A later page of one long report is reading it, not checking on the agent.
    reads = sum(t.name == _READ and not _PAGED.search(t.args) for t in evidence.tools("parent"))
    polling = (
        [f"{reads} reads of {len(evidence.subagents)} sub-agents"]
        if reads > max(1, len(evidence.subagents))
        else []
    )
    return [
        _implicit("every turn ended done", unfinished),
        _implicit("no run error", errors),
        _implicit("no platform tool failure", broken),
        _implicit("no sub-agent left running", running),
        _implicit("every sub-agent reported", silent),
        _implicit("a title was produced", [] if evidence.title else ["no title"]),
        _implicit("no sub-agent left waiting on its parent", waiting, kind="behavior"),
        _implicit("no polling with agents_read", polling, kind="behavior"),
    ]
