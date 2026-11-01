"""What a voice run recorded. The turn timeline it is built from is :mod:`evals.drive.timeline`."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

from evals.drive.timeline import (
    Approval,
    Error,
    Question,
    Segment,
    SubagentRecord,
    Tasks,
    Text,
    Timeline,
    Tool,
    Turn,
    redact_home,
)
from evals.drive.usage import Kind, Usage, total

__all__ = [
    "BARE",
    "CONTEXT_DIR",
    "RUN_FILE",
    "Approval",
    "Error",
    "Question",
    "Run",
    "Sample",
    "Segment",
    "SubagentRecord",
    "Tasks",
    "Text",
    "Timeline",
    "Tool",
    "Turn",
    "load_run",
    "redact_home",
]

#: The profile a run records when none was applied.
BARE = "bare"


class _Model(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")


class Sample(_Model):
    scenario: str
    sample: int
    conversation_id: str | None = None
    turns: list[Turn] = Field(default_factory=list)
    title: str | None = None
    #: The live title failed and this one came from the retitle route, which sends the
    #: same title prompt every operator turn rather than the first.
    title_regenerated: bool = False
    work_summary: str | None = None
    subagents: list[SubagentRecord] = Field(default_factory=list)
    #: Why the sample is incomplete, when it is: setup failed, a turn timed out.
    problem: str | None = None
    #: Token use and cost of the sample's model calls; empty for non-Anthropic runs.
    usage: dict[Kind, Usage] = Field(default_factory=dict)


class Run(_Model):
    group: str
    #: The blind name, ``A``, ``B``…; the model behind it is in the group's ``key.json``.
    name: str
    started_at: str
    prompt_fingerprint: str
    stub: bool
    #: The workspace profile the run had around it.
    profile: str = BARE
    #: The endpoint's provider adapter, kept out of the transcript so grading stays blind.
    #: None on runs recorded before it was kept.
    provider: str | None = None
    samples: list[Sample] = Field(default_factory=list)
    #: Calls made after the scenarios: late titles and work summaries.
    helper_usage: dict[Kind, Usage] = Field(default_factory=dict)
    #: The most prompt tokens sent in any one minute of the run.
    peak_tokens_per_minute: int | None = None

    def replies(self) -> dict[tuple[str, int], int]:
        """Each sample's Talos reply count, a sub-agent delivery's included."""
        return {(s.scenario, s.sample): len(s.turns) for s in self.samples}

    def usage(self) -> Usage:
        """The whole run's token use and cost."""
        parts = [u for s in self.samples for u in s.usage.values()]
        return total([*parts, *self.helper_usage.values()])


RUN_FILE = "run.json"

#: Inside a run directory: the exact request Talos received, one file per scenario.
CONTEXT_DIR = "context"


def load_run(run_dir: Path) -> Run:
    return Run.model_validate_json((run_dir / RUN_FILE).read_text())
