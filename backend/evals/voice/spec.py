"""The scenario file: what each scenario says to Talos and what a grader looks for."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Literal

from pydantic import Field, TypeAdapter, model_validator

from evals.drive.spec import KEY_FILE, PROJECT_TOKEN, Level, Memory, Strict, resolve_run_path

from .grading import DIMENSIONS, IMPLICIT, Dimension

__all__ = [
    "FIXTURES_DIR",
    "KEY_FILE",
    "PROJECT_TOKEN",
    "RUNS_DIR",
    "SCENARIOS_PATH",
    "Level",
    "Memory",
    "Scenario",
    "ScenarioFile",
    "Voice",
    "load_scenarios",
    "run_path",
]

type Voice = Literal["talos", "working", "subagent"]

SCENARIOS_PATH = Path(__file__).with_name("scenarios.toml")
FIXTURES_DIR = Path(__file__).with_name("fixtures")
RUNS_DIR = Path(__file__).with_name("runs")


def run_path(raw: str) -> Path:
    """A run-group or run directory, absolute or relative to :data:`RUNS_DIR`."""
    return resolve_run_path(raw, RUNS_DIR)


class Scenario(Strict):
    id: str
    voice: Voice
    #: The first message, then each follow-up once the previous turn has ended.
    prompts: list[str] = Field(min_length=1)
    #: What the scenario probes, in a line, for the grader.
    about: str
    dimensions: list[Dimension] = Field(min_length=1)
    #: Scenario-specific checks, each a sentence a grader marks pass or fail.
    expect: list[str] = Field(min_length=1)
    requires_vm: bool = False
    #: ``all`` approves every parked call; ``none`` denies with a note to find another way.
    approve: Literal["all", "none"] = "all"
    permission_level: Level | None = None
    memories: list[Memory] = Field(default_factory=list)
    #: §6 wants no line here (a failure, a frustrated operator, a correction), so the
    #: humor tally doesn't count its silence as a missed line.
    quiet: bool = False
    #: A directory under ``fixtures/``, copied fresh per sample and opened as the
    #: thread's project.
    fixture: str | None = None
    timeout_s: float = 900.0

    @property
    def graded(self) -> list[Dimension]:
        """The listed dimensions plus the implicit ones, in rubric order."""
        return [d for d in DIMENSIONS if d in self.dimensions or d in IMPLICIT]

    @model_validator(mode="after")
    def _consistent(self) -> Scenario:
        uses_token = any(PROJECT_TOKEN in prompt for prompt in self.prompts)
        if uses_token and self.fixture is None:
            raise ValueError(f"{self.id}: prompts name {PROJECT_TOKEN} but no fixture is set")
        if self.voice != "talos" and not self.requires_vm:
            raise ValueError(f"{self.id}: a {self.voice} scenario needs the VM")
        if listed := sorted(set(self.dimensions) & set(IMPLICIT)):
            raise ValueError(f"{self.id}: {', '.join(listed)} are scored on every sample")
        return self


class ScenarioFile(Strict):
    scenario: list[Scenario]


_FILE: TypeAdapter[ScenarioFile] = TypeAdapter(ScenarioFile)


def load_scenarios(path: Path = SCENARIOS_PATH) -> list[Scenario]:
    """Every scenario in file order; ids must be unique and fixtures must exist."""
    scenarios = _FILE.validate_python(tomllib.loads(path.read_text())).scenario
    ids = [s.id for s in scenarios]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        raise ValueError(f"duplicate scenario ids: {', '.join(duplicates)}")
    missing = sorted(
        s.fixture for s in scenarios if s.fixture and not (FIXTURES_DIR / s.fixture).is_dir()
    )
    if missing:
        raise ValueError(f"missing fixtures: {', '.join(missing)}")
    return scenarios
