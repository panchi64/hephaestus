"""What every suite's scenario file and run directories have in common."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict

type Level = Literal["plan", "manual", "edit", "auto", "yolo"]

#: In a run group: which model each blind run letter stands for.
KEY_FILE = "key.json"

#: Replaced in prompts by the fixture project's home path, ``~/projects/<slug>``.
PROJECT_TOKEN = "{project}"


def resolve_run_path(raw: str, runs_dir: Path) -> Path:
    """A run-group or run directory, absolute or relative to ``runs_dir``."""
    path = Path(raw)
    return path if path.is_absolute() or path.exists() else runs_dir / raw


class Strict(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)


class Memory(Strict):
    """A memory seeded before a sample and removed after it."""

    content: str
    description: str
    pinned: bool = True
