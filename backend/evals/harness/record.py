"""What a harness run recorded."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

from evals.drive.timeline import SubagentRecord, Turn
from evals.drive.usage import Kind, Usage, total

from .evidence import ProxiedRequest
from .implicit import CheckResult

RUN_FILE = "run.json"
TRANSCRIPT_FILE = "transcript.md"
#: Inside a run directory: the exact first request each scenario's agent received.
CONTEXT_DIR = "context"


class _Model(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")


class Sample(_Model):
    scenario: str
    sample: int
    conversation_id: str | None = None
    turns: list[Turn] = Field(default_factory=list)
    #: The ``new_thread`` prompts' own thread.
    later_conversation_id: str | None = None
    later: list[Turn] = Field(default_factory=list)
    subagents: list[SubagentRecord] = Field(default_factory=list)
    title: str | None = None
    tasks: list[tuple[str, str]] = Field(default_factory=list)
    #: The mid-run actions that fired, in order, as one line each.
    actions: list[str] = Field(default_factory=list)
    requests: list[ProxiedRequest] = Field(default_factory=list)
    checks: list[CheckResult] = Field(default_factory=list)
    #: Why the sample is incomplete, when it is.
    problem: str | None = None
    usage: dict[Kind, Usage] = Field(default_factory=dict)

    def failed_checks(self) -> list[CheckResult]:
        return [c for c in self.checks if not c.passed]


class Routing(_Model):
    """The model settings the run had around every scenario that didn't set its own."""

    local: str = ""
    hosted: str = ""
    subagent: str = ""
    choosable: list[str] = Field(default_factory=list)


class Run(_Model):
    group: str
    name: str
    started_at: str
    prompt_fingerprint: str
    stub: bool
    provider: str | None = None
    routing: Routing = Field(default_factory=Routing)
    samples: list[Sample] = Field(default_factory=list)
    #: Scenarios left out, each with why.
    skipped: dict[str, str] = Field(default_factory=dict)
    helper_usage: dict[Kind, Usage] = Field(default_factory=dict)
    peak_tokens_per_minute: int | None = None

    def usage(self) -> Usage:
        parts = [u for s in self.samples for u in s.usage.values()]
        return total([*parts, *self.helper_usage.values()])


def load_run(run_dir: Path) -> Run:
    return Run.model_validate_json((run_dir / RUN_FILE).read_text())
