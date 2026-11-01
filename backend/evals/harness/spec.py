"""The scenario file: what each scenario asks the harness to do, and how it is scored."""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import Field, TypeAdapter, model_validator

from evals.drive.spec import KEY_FILE, PROJECT_TOKEN, Level, Memory, resolve_run_path
from evals.drive.spec import Strict as _Strict

from .checks import Check
from .grading import DIMENSIONS, IMPLICIT, Dimension

__all__ = [
    "FIXTURES_DIR",
    "HOSTED_TOKEN",
    "KEY_FILE",
    "LOCAL_TOKEN",
    "PROJECT_TOKEN",
    "RUNS_DIR",
    "SCENARIOS_PATH",
    "Action",
    "Area",
    "Choice",
    "Level",
    "Memory",
    "Pin",
    "Requirement",
    "Routing",
    "Scenario",
    "ScenarioFile",
    "StubCall",
    "StubReply",
    "fill",
    "load_scenarios",
    "run_path",
]

type Area = Literal[
    "main", "tools", "delegation", "intercom", "subagent", "models", "solving", "flow"
]
#: ``search`` needs the VM as well: web search is SearXNG in the services VM.
type Requirement = Literal["vm", "search"]

SCENARIOS_PATH = Path(__file__).with_name("scenarios.toml")
FIXTURES_DIR = Path(__file__).with_name("fixtures")
RUNS_DIR = Path(__file__).with_name("runs")

#: Replaced in routing settings by the run's ``--local`` and ``--hosted`` models.
LOCAL_TOKEN = "{local}"
HOSTED_TOKEN = "{hosted}"

_TRIGGER = re.compile(r"^(first_tool|tool:[a-z_]+|seconds:\d+(\.\d+)?)$")


def fill(text: str, tokens: dict[str, str]) -> str:
    for token, value in tokens.items():
        text = text.replace(token, value)
    return text


def run_path(raw: str) -> Path:
    """A run-group or run directory, absolute or relative to :data:`RUNS_DIR`."""
    return resolve_run_path(raw, RUNS_DIR)


class Action(_Strict):
    """A step taken while a turn runs, the first time ``at`` matches."""

    #: ``first_tool``, ``tool:<name prefix>`` or ``seconds:<N>`` after the turn started.
    at: str
    do: Literal["steer", "cancel"]
    #: The message a steer sends into the running turn.
    text: str = ""
    #: Which prompt's turn, from 1.
    turn: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def _valid(self) -> Action:
        if not _TRIGGER.match(self.at):
            raise ValueError(f"action trigger {self.at!r} is not first_tool, tool:<p>, seconds:N")
        if self.do == "steer" and not self.text.strip():
            raise ValueError("a steer needs text")
        return self


class StubCall(_Strict):
    name: str
    arguments: dict[str, object] = Field(default_factory=dict)


class StubReply(_Strict):
    """One scripted stub reply (``devkit/stub/scripts.py``), matched on the last user
    message."""

    match: str | None = None
    text: str = ""
    tool_calls: list[StubCall] = Field(default_factory=list)
    needs_tools: bool | None = None
    uses: int | None = None
    latency_s: float = 0.0

    def wire(self, tokens: dict[str, str], index: int) -> dict[str, object]:
        """The body ``/_stub/script`` takes, with each token in ``tokens`` filled in.

        ``index`` is the reply's place in the script; it keeps each call's id unique, since
        a run keys its tool cards by id."""
        out = self.model_dump(exclude={"tool_calls"}, exclude_none=True)
        out["tool_calls"] = [
            {
                "name": call.name,
                "id": f"call_{index}_{n}",
                "arguments": fill(json.dumps(call.arguments), tokens),
            }
            for n, call in enumerate(self.tool_calls)
        ]
        if self.match is not None:
            out["match"] = fill(self.match, tokens)
        out["text"] = fill(self.text, tokens)
        return out


class Choice(_Strict):
    #: ``<endpoint name>/<model>``, or :data:`LOCAL_TOKEN` / :data:`HOSTED_TOKEN`.
    ref: str
    note: str = ""


class Pin(_Strict):
    #: The agent's id, ``builtin:reviewer`` and the like.
    agent: str
    #: As :attr:`Choice.ref`.
    ref: str


class Routing(_Strict):
    """Model settings a scenario runs under, applied for its samples and restored after."""

    #: Replaces the run's choosable list; ``[]`` offers none. Absent keeps the run's.
    choosable: list[Choice] | None = None
    #: Binds the ``subagent`` role; ``""`` unbinds it. Absent keeps the run's.
    subagent: str | None = None
    pins: list[Pin] = Field(default_factory=list)

    def refs(self) -> list[str]:
        out = [c.ref for c in self.choosable or []] + [p.ref for p in self.pins]
        return [*out, self.subagent] if self.subagent else out


class Scenario(_Strict):
    id: str
    area: Area
    #: What the scenario probes, in a line, for the grader.
    about: str
    #: The first message, then each follow-up once the previous turn has ended.
    prompts: list[str] = Field(min_length=1)
    #: Sent afterwards in a fresh thread, to test what carries across threads.
    new_thread: list[str] = Field(default_factory=list)
    actions: list[Action] = Field(default_factory=list)
    permission_level: Level | None = None
    #: ``all`` approves every parked call the eval may approve; ``none`` refuses them all.
    approve: Literal["all", "none"] = "all"
    #: Tool prefixes approvable beyond the VM-only ones, for instance-local tools.
    allow: list[str] = Field(default_factory=list)
    #: Tool prefixes refused even under ``approve = "all"``: ``plan_submit`` rejects a plan.
    deny: list[str] = Field(default_factory=list)
    #: What each question the agent asks is answered with; absent for "use your judgement".
    answer: str | None = None
    requires: list[Requirement] = Field(default_factory=list)
    #: A directory under ``fixtures/``, copied fresh per sample and opened as the project.
    fixture: str | None = None
    #: Initialise the fixture copy as a git repository with one commit.
    git: bool = False
    memories: list[Memory] = Field(default_factory=list)
    routing: Routing | None = None
    timeout_s: float = 900.0
    checks: list[Check] = Field(default_factory=list)
    #: Sentences a grader marks pass or fail.
    expect: list[str] = Field(min_length=1)
    dimensions: list[Dimension] = Field(min_length=1)
    #: Scripted replies for a run on the dev stub; a scenario without them is skipped there.
    stub: list[StubReply] = Field(default_factory=list)

    @property
    def graded(self) -> list[Dimension]:
        """The listed dimensions plus the implicit ones, in rubric order."""
        return [d for d in DIMENSIONS if d in self.dimensions or d in IMPLICIT]

    @model_validator(mode="after")
    def _consistent(self) -> Scenario:
        texts = [*self.prompts, *self.new_thread, *(r.text for r in self.stub)]
        if any(PROJECT_TOKEN in t for t in texts) and self.fixture is None:
            raise ValueError(f"{self.id}: {PROJECT_TOKEN} is used but no fixture is set")
        if self.git and self.fixture is None:
            raise ValueError(f"{self.id}: git needs a fixture")
        if listed := sorted(set(self.dimensions) & set(IMPLICIT)):
            raise ValueError(f"{self.id}: {', '.join(listed)} are scored on every sample")
        if late := [a.turn for a in self.actions if a.turn > len(self.prompts)]:
            raise ValueError(f"{self.id}: an action names turn {late[0]} past the prompts")
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
