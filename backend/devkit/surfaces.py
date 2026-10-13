"""Writing the files a future session will actually read.

Both surfaces a fresh Claude Code session reads — ``.claude/`` and ``CLAUDE.md`` — are
gitignored, on purpose. That has a consequence worth stating plainly, because it is the
whole reason this module exists: **writing them by hand solves this session and nothing
after it.** A new clone does not have them. A new git worktree does not have them, and
this project is developed in several worktrees at once, so a fresh worktree is the
ordinary case rather than the rare one.

So ``docs/dev-instance.md`` is committed and is the source of truth, and these are
materialized from it on every ``up``. A fresh checkout gets them the first time anyone
starts an instance, and they are rewritten afterwards — which matters more than it sounds,
since they carry this instance's ports and a stale copy would aim a session at another
worktree's instance.

Writes are confined to the files named here, and the ``CLAUDE.md`` one is a delimited
block replaced in place: the operator's own notes live in that file too, and a generator
that rewrote it wholesale would be a generator nobody could leave switched on.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from devkit import repo
from devkit.instance import DevInstance

#: The committed recipe these are generated from.
SOURCE = repo.ROOT / "docs" / "dev-instance.md"

#: The stanza inside it that belongs in CLAUDE.md, and the markers that delimit the block
#: in CLAUDE.md itself. The same names, because they mark the same passage at both ends.
_BEGIN = "<!-- claude-md:begin -->"
_END = "<!-- claude-md:end -->"

#: What the skill is called, and so what a session finds when it looks for a way to run
#: the app. The built-in `run` skill checks for a project skill covering app launch first.
SKILL_NAME = "dev-instance"

_SKILL_FRONTMATTER = """---
name: {skill}
description: >-
  Run this project's app to see a change working — an isolated dev instance with its own
  data, ports and a scripted model, separate from the operator's workspace. Use when
  asked to run, start, launch or preview the app, to screenshot or look at a screen, to
  verify a change visually or check it in the real product rather than only in tests, or
  to reproduce something end to end. Also covers seeding it, scripting the model's
  replies, injecting failures, and resetting it.
---

"""


def _substitute(text: str, instance: DevInstance, config_name: str) -> str:
    """Fill in this instance's own numbers, so the copy a session reads is about the
    instance it will actually get. The recipe still says to confirm them with
    ``status --json``, because a *second* worktree's copy will say something else."""
    for token, value in {
        "{{frontend_url}}": instance.frontend_url,
        "{{backend_url}}": instance.backend_url,
        "{{stub_port}}": str(instance.stub_port),
        "{{launch_config}}": config_name,
        "{{name}}": instance.name,
        "{{root}}": str(instance.root),
    }.items():
        text = text.replace(token, value)
    return text.replace("<worktree>", instance.name)


def _recipe() -> str:
    """The committed recipe, or a pointer to it if it has been moved or deleted.

    Degrading rather than failing: a missing document is not a reason for ``up`` to
    refuse to start an instance, and a session told where to look is better served than
    one told nothing because a file was renamed.
    """
    try:
        return SOURCE.read_text()
    except OSError:
        return (
            f"# The dev instance\n\n`{SOURCE}` is missing — it is the source this file is\n"
            "generated from. Run `uv run python dev_instance.py status --json` for the\n"
            "instance's ports, and `--help` for what the command can do.\n"
        )


def claude_md_stanza(text: str) -> str:
    """The passage between the markers — the short version, for the always-loaded file."""
    start, end = text.find(_BEGIN), text.find(_END)
    if start == -1 or end == -1 or end < start:
        return ""
    return text[start + len(_BEGIN) : end].strip()


def launch_config(instance: DevInstance, config_name: str) -> dict[str, object]:
    """The Claude Code launch configuration that opens this instance in the browser pane.

    It carries no command: ``up`` owns starting the services, and a second launcher racing
    it would fight over ports. An entry with only a ``url`` attaches to what is already
    running, which is the relationship wanted here.
    """
    return {
        "version": "0.0.1",
        "configurations": [
            {"name": config_name, "url": instance.frontend_url, "port": instance.frontend_port}
        ],
    }


def write_launch_json(instance: DevInstance, config_name: str, root: Path) -> Path:
    """Write ``.claude/launch.json``, preserving any configurations we do not own."""
    root.mkdir(parents=True, exist_ok=True)
    path = root / "launch.json"

    ours = launch_config(instance, config_name)
    configurations = list(ours["configurations"])  # type: ignore[arg-type]
    if path.is_file():
        try:
            existing = json.loads(path.read_text())
        except OSError, ValueError:
            existing = {}
        # Anything the operator added stays. Only the entry carrying our name is ours to
        # rewrite, and it has to be rewritten because it holds a port that can move.
        configurations += [
            entry
            for entry in existing.get("configurations", [])
            if isinstance(entry, dict) and entry.get("name") != config_name
        ]
    path.write_text(
        json.dumps({"version": "0.0.1", "configurations": configurations}, indent=2) + "\n"
    )
    return path


def write_skill(instance: DevInstance, config_name: str, root: Path) -> Path:
    """Write the project skill — the full recipe, where a session looking for one finds it."""
    directory = root / "skills" / SKILL_NAME
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "SKILL.md"
    body = _substitute(_recipe(), instance, config_name)
    path.write_text(_SKILL_FRONTMATTER.format(skill=SKILL_NAME) + body)
    return path


def write_claude_md(instance: DevInstance, config_name: str, path: Path) -> Path:
    """Insert or replace the pointer block in ``CLAUDE.md``, leaving the rest untouched.

    The short stanza rather than the recipe: this file is loaded into every session
    whether or not the instance is wanted, so it earns only enough words to say the thing
    exists, what it is for, and which command to run.
    """
    stanza = _substitute(claude_md_stanza(_recipe()), instance, config_name)
    if not stanza:
        return path
    block = f"{_BEGIN}\n\n{stanza}\n\n{_END}"

    existing = path.read_text() if path.is_file() else ""
    pattern = re.compile(re.escape(_BEGIN) + ".*?" + re.escape(_END), re.DOTALL)
    if pattern.search(existing):
        updated = pattern.sub(lambda _: block, existing, count=1)
    else:
        updated = f"{existing.rstrip()}\n\n{block}\n" if existing.strip() else f"{block}\n"
    path.write_text(updated)
    return path


def write_all(instance: DevInstance, root: Path | None = None) -> list[Path]:
    """Materialize every generated surface. Returns what was written, for reporting."""
    from devkit.launch import LAUNCH_CONFIG

    claude_dir = root or repo.CLAUDE_DIR
    return [
        write_launch_json(instance, LAUNCH_CONFIG, claude_dir),
        write_skill(instance, LAUNCH_CONFIG, claude_dir),
        write_claude_md(instance, LAUNCH_CONFIG, claude_dir.parent / "CLAUDE.md"),
    ]
