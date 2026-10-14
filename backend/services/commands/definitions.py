"""Commands a project declares for itself, in its own files.

A repository that has written down how *its* release notes get drafted knows something this
installation cannot, and the convention for writing it down already exists: a markdown file
with YAML frontmatter under ``.claude/commands/``, the shape Claude Code reads. So a project
command is not a fifth kind of thing. It is a :class:`CommandSpec` built from a file instead
of from a database row, and past this module nothing can tell the two apart.

This mirrors ``services/subagents/definitions`` beat for beat, on purpose — same asset scan,
same per-file skip, same name rule, same caps — because they are the same job over a
different directory, and a project whose ``agents/`` files load while its ``commands/`` files
quietly do not would be impossible to explain.

**A bad file is skipped, never fatal.** These are hand-written, they arrive with a checkout,
and nobody editing one is watching this process. A file that cannot be read costs its own
command and nothing else.

**Project folders only**, which is enforced by the spec's ``workspaces`` rather than here: a
sandbox thread has no repository to have declared anything, and a picker offering a command
whose body came from a checkout the thread cannot see would be offering nothing.
"""

from __future__ import annotations

import logging
from pathlib import Path

from pydantic_ai_harness.repo_context import RepoContext

from core.exceptions import CommandValidationError
from core.frontmatter import FrontmatterError, split_frontmatter

from .authoring import (
    ARGUMENT_HINT_MAX_CHARS,
    BODY_MAX_CHARS,
    DESCRIPTION_MAX_CHARS,
    TITLE_MAX_CHARS,
    clip,
    validate_name,
)
from .spec import CommandSpec

logger = logging.getLogger(__name__)

#: The roots scanned for ``commands/*.md``, taken from the capability's own default rather
#: than restated, so this and the repo-inventory tool describe one filesystem layout.
#:
#: Globbed directly rather than through ``scan_assets``, which the sub-agent scan next door
#: uses: that scan reports ``skills`` and ``agents`` and has no notion of a commands
#: directory, so going through it would mean reading a list that structurally cannot contain
#: the answer. Sharing the *roots* is what keeps the two in step, and that is shared here.
ASSET_ROOTS: tuple[str, ...] = tuple(RepoContext.asset_roots)

#: The directory whose name says what is in it, read flat as well as through the scan above
#: — a project that put ``standup.md`` straight in here meant a command by it.
FLAT_ROOT = ".commands"

#: Where a project-declared command is offered: the threads that work in the project's
#: files the command came from.
PROJECT_WORKSPACES: tuple[str, ...] = ("checkout", "worktree")


class CommandFileError(ValueError):
    """The file is not a usable command definition."""


def project_specs(root: Path) -> tuple[CommandSpec, ...]:
    """Every command this project declares, by name, in discovery order.

    Never raises. A project whose command files are all broken has the catalog it started
    with, which is the only behaviour that makes sense for files nobody is looking at while
    the operator is mid-keystroke.

    **First wins** on a name collision within the project, matching ``project_specs`` for
    sub-agents: two files claiming one name is a mistake in the checkout, and the tiebreak
    only has to be decided and stable — ``command_files`` sorts, so it is both.
    """
    specs: dict[str, CommandSpec] = {}
    for path in command_files(root):
        try:
            spec = parse_command_file(path.read_text(encoding="utf-8"), name=path.stem)
        except (CommandFileError, OSError, UnicodeDecodeError) as exc:
            logger.info("commands: skipped command file %s (%s)", path, exc)
            continue
        _ = specs.setdefault(spec.name, spec)
    return tuple(specs.values())


def command_files(root: Path) -> list[Path]:
    """The command definition files under a project root, deduplicated and ordered.

    Sorted by path rather than left in scan order, so two checkouts of one repository
    produce the same menu in the same order.
    """
    found: set[Path] = set()
    for directory in [*(root / name / "commands" for name in ASSET_ROOTS), root / FLAT_ROOT]:
        if directory.is_dir():
            found.update(path.resolve() for path in directory.glob("*.md") if path.is_file())
    return sorted(found)


def parse_command_file(text: str, *, name: str) -> CommandSpec:
    """One command definition file as a spec.

    ``name`` is the filename's stem, which is what the standard actually addresses a command
    by — ``.claude/commands/standup.md`` is ``/standup``. Frontmatter may override it, for
    the case where the file is named for its contents rather than for its handle.

    Frontmatter is **optional here**, unlike an agent file — which is why a
    ``FrontmatterError`` reads as "no frontmatter" rather than as a broken file. A command is
    a prompt; the smallest useful one in the wild is a body with nothing above it, and
    demanding a description for a name only the operator can type would be ceremony for its
    own sake. An agent file cannot do the same, because its description is the only thing the
    model reads when choosing one — nobody chooses a command but the person typing it.

    What is absent falls back to something honest rather than to a placeholder: the
    description defaults to the file's first line, which is what a prompt template's first
    line almost always already is.
    """
    fields: dict[str, object]
    try:
        fields, body = split_frontmatter(text)
    except FrontmatterError:
        fields, body = {}, text

    spec_name = _name(fields.get("name"), fallback=name)
    template = body.strip()
    if not template:
        # The one field with no fallback: a command whose body is empty is a name that does
        # nothing when typed, and the picker would be offering it for no reason.
        raise CommandFileError(f"command {spec_name!r} has no template")
    description = _line(fields.get("description")) or _first_line(template)

    return CommandSpec(
        name=spec_name,
        source="project",
        kind="prompt",
        title=clip(_line(fields.get("title")) or spec_name, TITLE_MAX_CHARS),
        description=clip(description, DESCRIPTION_MAX_CHARS),
        # The standard's own key, and ours by the same name. A hint, never enforced.
        argument_hint=clip(_line(fields.get("argument-hint")), ARGUMENT_HINT_MAX_CHARS) or None,
        body=clip(template, BODY_MAX_CHARS),
        workspaces=PROJECT_WORKSPACES,
    )


def _name(raw: object, *, fallback: str) -> str:
    try:
        return validate_name(_line(raw) or fallback)
    except CommandValidationError as exc:
        # Translated, not propagated: everything above this reads a bad file as one skipped
        # command, and it recognises that by the file error alone.
        raise CommandFileError(str(exc)) from None


def _line(raw: object) -> str:
    """A frontmatter scalar as one line of text, or ``""`` for anything that is not one.

    Wrong types read as absent rather than raising, because every field this is used for has
    a fallback — losing a whole command over a ``description:`` someone wrote as a list would
    be a worse answer than showing the template's first line instead.
    """
    if not isinstance(raw, str):
        return ""
    return " ".join(raw.split()).strip()


def _first_line(body: str) -> str:
    """The template's opening line, as the description of last resort.

    Markdown heading marks are taken off because a file that opens ``# Draft the release
    notes`` means the words, not the hashes, and the picker renders no markdown.
    """
    for line in body.splitlines():
        stripped = line.strip().lstrip("#").strip()
        if stripped:
            return stripped
    return ""
