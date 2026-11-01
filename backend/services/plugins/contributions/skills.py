"""``skills``: skill bundles from ``skills/<name>/SKILL.md`` and the files beside it."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import override

from core.exceptions import PluginValidationError, SkillValidationError
from services.skills.bundle import SKILL_FILE, ParsedSkill, parse_skill_md

from .base import BundleSource, Described, TypedContribution
from .registry import CONTRIBUTIONS


@dataclass(frozen=True)
class PluginSkill:
    skill: ParsedSkill
    #: The skill's bundled files, by path within the skill.
    files: tuple[tuple[str, bytes], ...]


class Skills(TypedContribution[PluginSkill]):
    @override
    def parse(self, source: BundleSource) -> tuple[PluginSkill, ...]:
        if source.declared:
            raise PluginValidationError(self.key, "skills come from skills/<name>/SKILL.md files")
        files: Mapping[str, bytes] = source.files or {}
        skills: list[PluginSkill] = []
        for path, blob in sorted(files.items()):
            parts = path.split("/")
            if len(parts) != 3 or parts[0] != "skills" or parts[2] != SKILL_FILE:
                continue
            prefix = f"skills/{parts[1]}/"
            try:
                parsed = parse_skill_md(blob.decode())
            except (SkillValidationError, UnicodeDecodeError) as exc:
                raise PluginValidationError("skills", f"{path}: {exc}") from None
            bundled = tuple(
                (other.removeprefix(prefix), content)
                for other, content in sorted(files.items())
                if other.startswith(prefix) and other != path
            )
            skills.append(PluginSkill(parsed, bundled))
        if len({entry.skill.name for entry in skills}) != len(skills):
            raise PluginValidationError("skills", "two skills share a name")
        return tuple(skills)

    @override
    def describe_one(self, item: PluginSkill) -> Described:
        return {"name": item.skill.name}


SKILLS = CONTRIBUTIONS.register(Skills("skills", "Skills", PluginSkill))
