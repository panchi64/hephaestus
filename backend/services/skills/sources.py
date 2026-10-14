"""Skills read from disk: the platform's own, shipped with the code, and a project's.

Both are read-only bundles, a ``SKILL.md`` and the files beside it. Platform skills live
in ``backend/skills/<name>/``; a project's in ``<root>/.claude/skills/<name>/`` (or any
other asset root project agents are read from).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from core.exceptions import SkillValidationError
from services.agents.files import ASSET_ROOTS

from .bundle import BUNDLE_MAX_BYTES, SKILL_FILE, ParsedSkill, parse_skill_md

logger = logging.getLogger(__name__)

PLATFORM_ROOT = Path(__file__).resolve().parents[2] / "skills"


@dataclass(frozen=True)
class DiskSkill:
    skill: ParsedSkill
    #: The files beside ``SKILL.md``, by path within the skill.
    files: tuple[tuple[str, bytes], ...] = ()


def read_skill_dir(directory: Path) -> DiskSkill:
    """One skill directory. Raises :class:`SkillValidationError` or :class:`OSError`."""
    skill = parse_skill_md((directory / SKILL_FILE).read_text(encoding="utf-8"))
    files: list[tuple[str, bytes]] = []
    total = 0
    for path in sorted(directory.rglob("*")):
        relpath = path.relative_to(directory).as_posix()
        # Symlinks are skipped so a project skill cannot stage files from outside its tree.
        if path.is_symlink() or not path.is_file() or relpath == SKILL_FILE:
            continue
        if relpath.startswith("__pycache__/") or "/__pycache__/" in relpath:
            continue
        blob = path.read_bytes()
        total += len(blob)
        if total > BUNDLE_MAX_BYTES:
            raise SkillValidationError("files", "the bundle is too large")
        files.append((relpath, blob))
    return DiskSkill(skill, tuple(files))


def read_skills(directories: list[Path]) -> tuple[DiskSkill, ...]:
    """Every readable skill among ``directories``, first one wins a name. Never raises."""
    found: dict[str, DiskSkill] = {}
    for directory in directories:
        if directory.is_symlink() or not (directory / SKILL_FILE).is_file():
            continue
        try:
            entry = read_skill_dir(directory)
        except (SkillValidationError, OSError, UnicodeDecodeError) as exc:
            logger.info("skills: skipped %s (%s)", directory, exc)
            continue
        _ = found.setdefault(entry.skill.name, entry)
    return tuple(found.values())


@cache
def platform_skills() -> tuple[DiskSkill, ...]:
    """The skills shipped with the app, read once."""
    if not PLATFORM_ROOT.is_dir():
        return ()
    return read_skills(sorted(PLATFORM_ROOT.iterdir()))


def project_skills(root: Path) -> tuple[DiskSkill, ...]:
    """The skills a project declares, in a stable order."""
    directories: list[Path] = []
    for name in ASSET_ROOTS:
        base = root / name / "skills"
        if base.is_dir() and not base.is_symlink():
            directories += sorted(base.iterdir())
    return read_skills(directories)
