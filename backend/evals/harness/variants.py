"""Trial variants: a registered prohibition removed, replaced or added for one arm of a run.

A variant file (``variants/<name>.toml``) names its edits by register id
(``prompts/register/``), so a reworded boundary can't leave a trial testing stale text:

    [[op]]
    boundary = "B-083"
    action = "remove"          # or "replace", with `text`

    [[op]]
    add = "…"                  # a boundary on trial, not yet registered
    after = "B-084"            # inserted after that entry's sentence

Each op resolves to the rewrites the backend applies (``agent/variant.py``) and to the probes
:mod:`evals.harness.compare` checks in the recorded requests: a probe that should be absent
from an arm and present in another is what shows the trial tested anything. Probes are
matched in the system prompt, the instructions and the tool definitions only, so an edit to
a skill's text can't be shown to have landed.

Each letter's arm and probes are kept in the group's ``key.json`` beside its model, out of
the run directory a blind grader reads.
"""

from __future__ import annotations

import json
import os
import tomllib
from pathlib import Path
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, TypeAdapter, model_validator

from evals.drive.spec import KEY_FILE
from prompts.boundaries import register

VARIANTS_DIR = Path(__file__).with_name("variants")
BASE = "base"


class _Strict(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)


class Op(_Strict):
    boundary: str | None = None
    action: Literal["remove", "replace"] | None = None
    text: str = ""
    add: str = ""
    after: str | None = None

    @model_validator(mode="after")
    def _one_shape(self) -> Op:
        editing = self.boundary is not None and self.action is not None
        adding = bool(self.add) and self.after is not None
        if editing == adding:
            raise ValueError("an op is either `boundary` + `action` or `add` + `after`")
        if self.action == "replace" and not self.text:
            raise ValueError("a `replace` op needs `text`")
        return self


class Probe(_Strict):
    """A sentence whose presence in the requests an arm sent says whether the edit landed."""

    text: str
    #: Present in the variant arm, or absent from it.
    present: bool


class Variant(_Strict):
    name: str
    ops: tuple[Op, ...]

    def rewrites(self) -> list[dict[str, str]]:
        """The backend's form: ``find``/``replace`` pairs with register ids resolved."""
        found = register()
        edits: list[dict[str, str]] = []
        for op in self.ops:
            if op.after is not None:
                anchor = found.by_id(op.after).text
                edits.append({"find": anchor, "replace": f"{anchor} {op.add}"})
            else:
                assert op.boundary is not None
                edits.append({"find": found.by_id(op.boundary).text, "replace": op.text})
        return edits

    def probes(self) -> list[Probe]:
        found = register()
        probes: list[Probe] = []
        for op in self.ops:
            if op.after is not None:
                probes.append(Probe(text=op.add, present=True))
                continue
            assert op.boundary is not None
            probes.append(Probe(text=found.by_id(op.boundary).text, present=False))
            if op.text:
                probes.append(Probe(text=op.text, present=True))
        return probes


def load_variant(name: str, directory: Path = VARIANTS_DIR) -> Variant:
    """Raises ``FileNotFoundError`` for an unknown name and ``KeyError`` for an op naming an
    id the register doesn't hold, so a broken trial fails before any model is called."""
    table: dict[str, object] = tomllib.loads((directory / f"{name}.toml").read_text())
    variant = Variant.model_validate({"name": name, "ops": table.get("op", [])})
    _ = variant.rewrites()
    return variant


def write_arm(path: Path, variant: Variant | None) -> None:
    """Point the backend at ``variant``'s rewrites, or at none for the base arm."""
    rewrites = variant.rewrites() if variant else []
    staged = path.with_name(f"{path.name}.tmp")
    _ = staged.write_text(json.dumps({"rewrite": rewrites}, indent=2) + "\n")
    # The backend reads the file on every request, so it must never see half of one.
    os.replace(staged, path)


class ArmKey(_Strict):
    """One letter of a group's ``key.json``."""

    model: str | None = None
    arm: str = BASE
    #: What the arm's requests must show (:class:`Probe`).
    probes: tuple[Probe, ...] = ()


_KEY = TypeAdapter(dict[str, ArmKey | str | None])


def load_key(group_dir: Path) -> dict[str, ArmKey]:
    """A group's letters; a letter keyed to a bare model name is a base arm."""
    path = group_dir / KEY_FILE
    if not path.is_file():
        return {}
    return {
        letter: value if isinstance(value, ArmKey) else ArmKey(model=value)
        for letter, value in _KEY.validate_json(path.read_text()).items()
    }
