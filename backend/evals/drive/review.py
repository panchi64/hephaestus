"""What every suite's review step shares: the files it reads and writes, the expect-item
correction, keeping the first pass, and the CLI loop."""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path

from pydantic import Field

from .spec import Strict

REVIEW_FILE = "review.json"
FIRST_FILE = "grades.first.json"
REPORT_FILE = "review.md"


class ExpectFix(Strict):
    scenario: str
    sample: int = Field(ge=1)
    #: The expect item, from 1, in the scenario's order.
    item: int = Field(ge=1)
    passed: bool
    note: str = ""
    reason: str = Field(min_length=1)


def first_pass(run_dir: Path, grades_file: str) -> Path:
    """The grader's own grades, kept aside the first time a review is applied, so applying
    it again never stacks corrections."""
    first = run_dir / FIRST_FILE
    if not first.is_file():
        _ = shutil.copyfile(run_dir / grades_file, first)
    return first


def review_main(
    argv: list[str],
    usage: str | None,
    review: Callable[[Path], list[str]],
    to_dir: Callable[[str], Path],
) -> int:
    """Review each run named in ``argv``, printing ``reviewed`` or its problems."""
    if not argv:
        print(usage)
        return 2
    failed = False
    for raw in argv:
        run_dir = to_dir(raw)
        found = review(run_dir)
        failed = failed or bool(found)
        print(f"{run_dir}: {'reviewed' if not found else f'{len(found)} problems'}")
        for problem in found:
            print(f"  - {problem}")
    return 1 if failed else 0
