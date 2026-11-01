"""Checks on what a finished eval run left behind, shared by every suite that scores one."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar, Literal, Protocol

from pydantic import BaseModel, ConfigDict

#: ``sandbox`` is the thread's ``/work``; ``project`` is the fixture copy it was opened on.
type Root = Literal["sandbox", "project"]


class Answered(Protocol):
    """What a check reads: the final answer, and files under a root."""

    @property
    def answer(self) -> str: ...

    def file(self, root: Root, path: str) -> Path | None:
        """``path`` under ``root`` on the host, or None when the run has no such root."""
        ...


def mentions(haystack: str, needles: list[str]) -> list[bool]:
    return [needle.lower() in haystack.lower() for needle in needles]


class Check(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)


class AnswerContains(Check):
    """The final answer mentions these strings, case-insensitively."""

    kind: Literal["answer_contains"] = "answer_contains"
    text: list[str]
    mode: Literal["all", "any"] = "all"

    def passes(self, evidence: Answered) -> bool:
        hits = mentions(evidence.answer, self.text)
        return all(hits) if self.mode == "all" else any(hits)

    def label(self) -> str:
        return f"answer {self.mode} of {self.text}"


class FileExists(Check):
    """A file exists, at a path relative to ``root``."""

    kind: Literal["file_exists"] = "file_exists"
    path: str
    root: Root = "sandbox"

    def passes(self, evidence: Answered) -> bool:
        target = evidence.file(self.root, self.path)
        return target is not None and target.is_file()

    def label(self) -> str:
        return f"file {self.path}"


class FileContains(Check):
    """A file under ``root`` contains these strings, case-insensitively."""

    kind: Literal["file_contains"] = "file_contains"
    path: str
    text: list[str]
    root: Root = "sandbox"

    def passes(self, evidence: Answered) -> bool:
        target = evidence.file(self.root, self.path)
        return (
            target is not None
            and target.is_file()
            and all(mentions(target.read_text(errors="replace"), self.text))
        )

    def label(self) -> str:
        return f"{self.path} contains {self.text}"
