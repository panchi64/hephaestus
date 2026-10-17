"""The scenario file: what each scenario says to Talos and what a grader looks for."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from .grading import Dimension

type Voice = Literal["talos", "working", "subagent"]
type Level = Literal["plan", "manual", "edit", "auto", "yolo"]

SCENARIOS_PATH = Path(__file__).with_name("scenarios.toml")
FIXTURES_DIR = Path(__file__).with_name("fixtures")
RUNS_DIR = Path(__file__).with_name("runs")
#: In a run group: which model each blind run letter stands for.
KEY_FILE = "key.json"

#: Replaced in every prompt by the fixture project's home path, ``~/projects/<slug>``.
PROJECT_TOKEN = "{project}"


class _Strict(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)


class Memory(_Strict):
    content: str
    description: str
    pinned: bool = True


class Scenario(_Strict):
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
    #: A directory under ``fixtures/``, copied fresh per sample and opened as the
    #: thread's project.
    fixture: str | None = None
    timeout_s: float = 900.0

    @model_validator(mode="after")
    def _consistent(self) -> Scenario:
        uses_token = any(PROJECT_TOKEN in prompt for prompt in self.prompts)
        if uses_token and self.fixture is None:
            raise ValueError(f"{self.id}: prompts name {PROJECT_TOKEN} but no fixture is set")
        if self.voice != "talos" and not self.requires_vm:
            raise ValueError(f"{self.id}: a {self.voice} scenario needs the VM")
        return self


class ScenarioFile(_Strict):
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
