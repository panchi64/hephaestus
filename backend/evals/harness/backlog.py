"""The tracked harness issues backlog: its entries and the file it lives in."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from .grading import IssueKind, Principle, Severity

BACKLOG_PATH = Path(__file__).with_name("backlog.json")

type Status = Literal["open", "fixed", "wontfix", "regressed"]
STATUSES: tuple[Status, ...] = ("open", "regressed", "fixed", "wontfix")


class Strict(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")


class Entry(Strict):
    id: str
    fingerprint: str
    title: str
    kind: IssueKind
    severity: Severity
    area: str
    scenarios: list[str]
    first_seen: str
    last_seen: str
    occurrences: int = 1
    evidence: str
    suspect: str = ""
    gap: str = ""
    principle: Principle | None = None
    status: Status = "open"
    note: str = ""


class Backlog(Strict):
    entries: list[Entry] = Field(default_factory=list)
    #: The run groups already merged, so merging one again changes nothing.
    groups: list[str] = Field(default_factory=list)


_BACKLOG: TypeAdapter[Backlog] = TypeAdapter(Backlog)


def read_backlog(path: Path = BACKLOG_PATH) -> Backlog:
    return _BACKLOG.validate_json(path.read_text()) if path.is_file() else Backlog()
