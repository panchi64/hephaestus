"""The register of prohibitions in model-facing text (``register/*.toml``).

A principle carries a whole class of behavior; a prohibition is kept only where the principle
cannot, and its entry says why. ``tests/test_prompt_boundaries.py`` keeps the register and
the text in step, and ``docs/voice.md`` §16 says when a prohibition is earned.
"""

from __future__ import annotations

import re
import tomllib
from functools import cache
from pathlib import Path
from typing import ClassVar, Literal, cast

from pydantic import BaseModel, ConfigDict, Field

#: One TOML file per group of sources, each a list of ``[[boundary]]`` tables.
REGISTER = Path(__file__).with_name("register")

Kind = Literal["fact", "safety", "preference", "earned"]
Principle = Literal["decision", "knowledge", "state", "scope", "visibility", "cost"]

#: What can make a sentence a prohibition. Broad on purpose: a principle-phrased limit ("is
#: cut, not softened", "nothing dark") restricts as much as a "never", and a description
#: caught by it costs one `fact` entry.
_MARKER = re.compile(
    r"\b(?:never|don't|do not|doesn't|does not|must not|avoid|only|nothing|no|not|instead"
    + r"|unless|leave (?:it |them )?out|stays? out|rather than)\b",
    re.IGNORECASE,
)
#: A block break: a blank line, or a line opening a list item, a heading or a table row.
_BLOCK = re.compile(r"\n\s*\n|\n(?=\s*(?:[-*+] |\d+\. |#|\|))")
_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"`(*_])")


class Boundary(BaseModel):
    """One registered prohibition."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(pattern=r"^B-\d{3,}$")
    text: str
    source: str
    kind: Kind
    principle: Principle | None = None
    why: str = ""
    evidence: tuple[str, ...] = ()


class Register(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    boundary: tuple[Boundary, ...] = ()

    def by_id(self, boundary_id: str) -> Boundary:
        """Raises ``KeyError`` for an id the register doesn't hold."""
        for entry in self.boundary:
            if entry.id == boundary_id:
                return entry
        raise KeyError(boundary_id)


@cache
def register() -> Register:
    entries: list[object] = []
    for path in sorted(REGISTER.glob("*.toml")):
        table: dict[str, object] = tomllib.loads(path.read_text())
        found = table.get("boundary", [])
        assert isinstance(found, list), f"{path.name}: `boundary` is not an array of tables"
        entries += cast(list[object], found)
    return Register.model_validate({"boundary": entries})


def sentences(text: str) -> list[str]:
    """``text`` as whitespace-normalized sentences, list items and table rows."""
    found: list[str] = []
    for block in _BLOCK.split(text):
        flat = " ".join(block.split())
        found += [part for part in _SENTENCE.split(flat) if part]
    return found


def prohibitions(text: str) -> list[str]:
    """The sentences in ``text`` that forbid or restrict, normalized as :func:`sentences`."""
    return [sentence for sentence in sentences(text) if _MARKER.search(sentence)]


def normalized(text: str) -> str:
    """``text`` with its whitespace collapsed, the form registered sentences are matched in."""
    return " ".join(text.split())
