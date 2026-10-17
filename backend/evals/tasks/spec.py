"""The task file: what each task asks, what it may be granted, and how it is scored."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

type Area = Literal["build", "research", "admin", "personal"]
#: Both run in the platform VM: web search is SearXNG inside it.
type Requirement = Literal["vm", "search"]


@dataclass(frozen=True, slots=True)
class Evidence:
    """What a finished task left behind for its checks to read."""

    answer: str
    tools_completed: list[str]
    #: The thread's sandbox on the host, the model's ``/work``.
    workdir: Path


def _mentions(haystack: str, needles: list[str]) -> list[bool]:
    return [needle.lower() in haystack.lower() for needle in needles]


class _Check(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)


class AnswerContains(_Check):
    """The final answer mentions these strings, case-insensitively."""

    kind: Literal["answer_contains"] = "answer_contains"
    text: list[str]
    mode: Literal["all", "any"] = "all"

    def passes(self, evidence: Evidence) -> bool:
        hits = _mentions(evidence.answer, self.text)
        return all(hits) if self.mode == "all" else any(hits)

    def label(self) -> str:
        return f"answer {self.mode} of {self.text}"


class FileExists(_Check):
    """A file exists in the thread's sandbox, at a path relative to ``/work``."""

    kind: Literal["file_exists"] = "file_exists"
    path: str

    def passes(self, evidence: Evidence) -> bool:
        return (evidence.workdir / self.path).is_file()

    def label(self) -> str:
        return f"file {self.path}"


class FileContains(_Check):
    """A sandbox file contains these strings, case-insensitively."""

    kind: Literal["file_contains"] = "file_contains"
    path: str
    text: list[str]

    def passes(self, evidence: Evidence) -> bool:
        target = evidence.workdir / self.path
        return target.is_file() and all(_mentions(target.read_text(errors="replace"), self.text))

    def label(self) -> str:
        return f"{self.path} contains {self.text}"


class ToolUsed(_Check):
    """At least one tool whose name starts with ``prefix`` completed."""

    kind: Literal["tool_used"] = "tool_used"
    prefix: str

    def passes(self, evidence: Evidence) -> bool:
        return any(name.startswith(self.prefix) for name in evidence.tools_completed)

    def label(self) -> str:
        return f"tool {self.prefix}*"


type Check = Annotated[
    AnswerContains | FileExists | FileContains | ToolUsed, Field(discriminator="kind")
]


class Task(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)

    id: str
    area: Area
    #: The first message, then each follow-up once the previous turn has ended.
    prompts: list[str] = Field(min_length=1)
    #: ``all`` approves every parked call; ``none`` denies them, for tasks that must
    #: finish without reaching past the workspace.
    approve: Literal["all", "none"] = "all"
    requires: list[Requirement] = Field(default_factory=list)
    timeout_s: float = 900.0
    checks: list[Check] = Field(default_factory=list)
    #: Scored 0–2 by the judge when set, in addition to the checks.
    rubric: str | None = None


class TaskFile(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)

    task: list[Task]


_TASK_FILE: TypeAdapter[TaskFile] = TypeAdapter(TaskFile)

TASKS_PATH = Path(__file__).with_name("tasks.toml")


def load_tasks(path: Path = TASKS_PATH) -> list[Task]:
    """Every task in the file, in file order; ids must be unique."""
    tasks = _TASK_FILE.validate_python(tomllib.loads(path.read_text())).task
    ids = [task.id for task in tasks]
    duplicates = sorted({task_id for task_id in ids if ids.count(task_id) > 1})
    if duplicates:
        raise ValueError(f"duplicate task ids in {path.name}: {', '.join(duplicates)}")
    return tasks
